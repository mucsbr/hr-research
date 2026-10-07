"""Real PostgreSQL state/fences plus signed protocol fixtures; no candidate execution."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import hmac
import threading
from types import SimpleNamespace
import uuid

import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import func, select

from app.adapter_code_package import canonical_bytes, content_sha256
from app.adapter_code_release import publish_code_proposal
from app.adapter_code_service import advance_code_validation, create_code_proposal, generate_code_candidate
from app.adapter_code_runtime import ProgramRuntime, freeze_for_task
from app.adapter_program import prepare_program_input, validate_list_output
from app.adapter_runner_client import AdapterRunnerClient
from app.adapter_runtime_models import AdapterProgramExecution
from app.adapter_maintenance import activate_release, ensure_registry
from app.collection import CollectionError
from app.config import get_settings
from app.db import session_factory
from app.models import Company, ModelCall, SourceBinding, SourceRun, Subject, TaskEvent, TaskRun, now
from app.serialization import row
from app.official_checkpoint import OfficialCheckpointStore
import test_adapter_code_release as release_support
import test_adapter_code_service as code_support
import test_adapter_program as program_support


class RuntimeRunner:
    def __init__(self, reference, output):
        self.reference, self.output = reference, deepcopy(output)
        self.posts, self.lookups, self.gets = [], [], []
        self.runs = {}
        self.lock = threading.Lock()
        self.lose_post = False
        self.store_post = True
        self.pending = False
        self.status, self.error_code = "succeeded", None
        self.cleanup = True
        self.environment = reference["dependency_environment_sha256"]
        self.on_post = self.on_get = None
        self.token = "runtime-test-only-signature-" + "x" * 40
        self.client = AdapterRunnerClient(SimpleNamespace(
            adapter_runner_url="https://runner.example.invalid", adapter_runner_token=SecretStr(self.token),
            app_env="test", adapter_runner_runtime_digest=reference["runtime_sha256"]),
            transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    def submit_run(self, bundle_sha, operation, payload, *, idempotency_key):
        request = AdapterRunnerClient.run_request(bundle_sha, operation, payload, idempotency_key=idempotency_key)
        run = {"run_id": str(uuid.uuid4()), "status": "running", "request_sha256": content_sha256(request), "request": request}
        with self.lock:
            self.posts.append(request)
            if self.store_post:
                self.runs[idempotency_key] = run
        if self.on_post:
            self.on_post()
        if self.lose_post:
            self.lose_post = False
            raise CollectionError("runner_submit_unknown", "模拟响应丢失，未自动重发")
        return deepcopy(run)

    def lookup_run(self, key):
        with self.lock:
            self.lookups.append(key)
            return deepcopy(self.runs.get(key))

    def get_run(self, run_id):
        with self.lock:
            self.gets.append(run_id)
            stored = deepcopy(next(item for item in self.runs.values() if item["run_id"] == run_id))
        if self.on_get:
            callback, self.on_get = self.on_get, None
            callback()
        if self.pending:
            return {"run_id": run_id, "status": "running"}
        request = stored["request"]
        output = deepcopy(self.output) if self.status == "succeeded" else None
        result = {"run_id": run_id, "status": self.status, "output": output, "error_code": self.error_code}
        limits = {"network": "none", "read_only": True, "user": "65532:65532", "cap_drop": ["ALL"],
                  "no_new_privileges": True, "pids_limit": 32, "memory_bytes": 268435456,
                  "cpus": 1, "timeout_seconds": 30}
        receipt = {"run_id": run_id, "status": self.status, "error_code": self.error_code,
                   "request_sha256": content_sha256(request), "operation": request["operation"],
                   "input_sha256": content_sha256(request["input"]), "output_sha256": content_sha256(output),
                   "bundle_sha256": request["bundle_sha256"], "lock_sha256": self.reference["lock_sha256"],
                   "runtime_sha256": self.reference["runtime_sha256"], "dependency_environment_sha256": self.environment,
                   "dependency_installation_sha256": "c" * 64, "execution_started": True,
                   "cleanup": {"container_removed": self.cleanup, "process_group_terminated": self.cleanup},
                   "limits": limits, "actual_limits": limits, "exit_code": 0 if self.status == "succeeded" else 1,
                   "oom_killed": False, "signature_algorithm": "hmac-sha256-v1"}
        key = hmac.new(self.token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
        receipt["signature"] = hmac.new(key, canonical_bytes(receipt), hashlib.sha256).hexdigest()
        return {**result, "receipt": receipt}

    def verify_result(self, result, **kwargs):
        return self.client.verify_result(result, **kwargs)


def seed_runtime(db, kind="boss"):
    if kind == "official":
        registry = ensure_registry(db, "official")
        registry.code_agent_enabled = True
        proposal = create_code_proposal(db, "official", base_spec={**code_support.spec(), "kind": "official"},
                                        source_snapshot={}, evidence_cases=code_support.cases())
        db.commit()
        publisher = release_support.ReleaseRunner()
        generate_code_candidate(proposal.id, gateway=code_support.gateway({**code_support.candidate(), "kind": "official"}, []),
                                 client=publisher)
        for _ in range(3):
            advance_code_validation(proposal.id, client=publisher)
        db.refresh(proposal)
        db.refresh(registry)
    else:
        proposal, registry, publisher, _ = release_support.verified(db)
    old_json = registry.active_release_id
    published = publish_code_proposal(db, proposal.id, expected_active_release_id=old_json,
                                      expected_generation=registry.release_generation, client=publisher)
    subject = Subject(name="甲公司")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name="甲公司")
    db.add(company)
    db.flush()
    binding = (SourceBinding(company_id=company.id, kind="careers", entry_url="https://careers.example/jobs")
               if kind == "official" else SourceBinding(company_id=company.id, kind="boss", **program_support.BINDING))
    db.add(binding)
    db.flush()
    snapshot = row(binding)
    task = TaskRun(company_id=company.id, domain="recruitment", kind="company_collection", status="running", attempt=1,
                   started_at=now(), heartbeat_at=now(), config_snapshot={"sources_snapshot": [snapshot],
                                                                         "company_identity": program_support.IDENTITY})
    db.add(task)
    db.flush()
    source = SourceRun(task_id=task.id, binding_id=binding.id, status="running", scope_hash="runtime-test-scope")
    db.add(source)
    db.commit()
    descriptor = freeze_for_task(kind, task_id=task.id, binding_id=binding.id, expected_attempt=1)
    if kind == "official":
        context = prepare_program_input("official", snapshot, program_support.IDENTITY, operation="navigation",
            page_url=binding.entry_url, html='<a rel="next" href="?page=2">下一页</a>')
        link = next(item["id"] for item in context.request["links"] if item["purpose"] == "next")
        output = program_support.output(context, navigation=[{"type": "follow", "link_id": link}])
    else:
        context, output = program_support.json_case()
    runner = RuntimeRunner(descriptor.program, output)
    return SimpleNamespace(task=task, source=source, binding=binding, snapshot=snapshot, descriptor=descriptor,
                           context=context, output=output, runner=runner, registry=registry,
                           release=published, old_json=old_json, publisher=publisher)


def runtime(state, *, attempt=1, **kwargs):
    return ProgramRuntime(state.descriptor, task_id=state.task.id, source_run_id=state.source.id,
                          binding_snapshot=state.snapshot, expected_attempt=attempt, client=state.runner,
                          wait_seconds=kwargs.pop("wait_seconds", 2), **kwargs)


def execution(db):
    db.expire_all()
    return db.scalar(select(AdapterProgramExecution))


def test_real_pg_request_idempotency_and_first_party_acceptance(db):
    state = seed_runtime(db)
    adapter = runtime(state)
    assert not adapter.evidence()["verified"]
    parsed = adapter.invoke(state.context)
    assert parsed.records[0].title == "研发工程师"
    again = adapter.invoke(state.context)
    assert again.records[0].source_job_id == parsed.records[0].source_job_id
    stored = execution(db)
    assert stored.status == "accepted" and stored.error_code is None
    assert stored.request["input"] == state.context.request
    assert stored.adapter_fingerprint == state.descriptor.fingerprint
    assert len(state.runner.posts) == 1 and len(state.runner.gets) == 1
    assert adapter.evidence()["verified"] and adapter.evidence()["run_ids"] == [stored.remote_run_id]
    assert db.scalar(select(func.count()).select_from(TaskEvent)) == 0


def test_unknown_post_reenters_only_through_original_key_lookup(db):
    state = seed_runtime(db)
    state.runner.lose_post = True
    with pytest.raises(CollectionError) as error:
        runtime(state).invoke(state.context)
    assert error.value.code == "runner_submit_unknown"
    stored = execution(db)
    original_key = stored.request["idempotency_key"]
    assert stored.status == "submitting" and stored.error_code == "runner_submit_unknown"
    assert runtime(state).invoke(state.context).records
    stored = execution(db)
    assert stored.status == "accepted" and stored.request["idempotency_key"] == original_key
    assert state.runner.lookups == [original_key] and len(state.runner.posts) == 1


def test_unknown_post_absent_lookup_never_resubmits_and_records_bounded_wait(db):
    state = seed_runtime(db)
    state.runner.lose_post, state.runner.store_post = True, False
    with pytest.raises(CollectionError):
        runtime(state).invoke(state.context)
    with pytest.raises(CollectionError) as error:
        runtime(state, wait_seconds=0.02, poll_seconds=0.01).invoke(state.context)
    assert error.value.code == "runner_wait_timeout"
    stored = execution(db)
    assert stored.status == "submitting" and stored.error_code == "runner_wait_timeout"
    assert len(state.runner.posts) == 1 and 1 <= len(state.runner.lookups) <= 5


def test_two_runtime_claims_submit_one_remote_execution(db):
    state = seed_runtime(db)
    entered, release = threading.Event(), threading.Event()
    first, second = runtime(state), runtime(state)
    def hold():
        entered.set()
        assert release.wait(5)
    state.runner.on_post = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(first.invoke, state.context)
        try:
            assert entered.wait(5)
            assert second.invoke(state.context).records
        finally:
            release.set()
        assert future.result(timeout=5).records
    assert len(state.runner.posts) == 1 and execution(db).status == "accepted"


def test_new_attempt_reuses_same_pure_input_and_old_owner_cannot_commit(db):
    state = seed_runtime(db)
    entered, release = threading.Event(), threading.Event()
    old = runtime(state)
    def hold():
        entered.set()
        assert release.wait(5)
    state.runner.on_post = hold
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(old.invoke, state.context)
        try:
            assert entered.wait(5)
            with session_factory()() as changed:
                task = changed.get(TaskRun, state.task.id)
                task.attempt = 2
                changed.commit()
            new = runtime(state, attempt=2)
            assert new.invoke(state.context).records
        finally:
            release.set()
        with pytest.raises(CollectionError) as error:
            future.result(timeout=5)
    assert error.value.code == "stale_execution"
    stored = execution(db)
    assert stored.owner_attempt == 2 and stored.status == "accepted" and stored.error_code is None
    assert len(state.runner.posts) == 1 and new.evidence()["verified"] and not old.evidence()["verified"]


def test_frozen_task_keeps_old_composite_after_pointer_change_and_bad_new_active(db):
    state = seed_runtime(db)
    activate_release(db, state.old_json, rollback=True, expected_active_release_id=state.release.id,
                     expected_generation=state.registry.release_generation)
    db.commit()
    state.registry.active_release_id = None  # Fault in a later active selection must not invalidate a frozen old release.
    db.commit()
    frozen = freeze_for_task("boss", task_id=state.task.id, binding_id=state.binding.id, expected_attempt=1)
    assert frozen.release_id == state.descriptor.release_id and frozen.fingerprint == state.descriptor.fingerprint
    assert runtime(state).invoke(state.context).records
    assert execution(db).bundle_sha == state.descriptor.program["bundle_sha256"]


@pytest.mark.parametrize("change,expected", [("cancel", "cancelled"), ("binding", "source_changed"), ("time", "time_budget")])
def test_current_owner_stop_conditions_are_persisted_without_accepting(db, change, expected):
    state = seed_runtime(db)
    adapter = runtime(state)
    def alter():
        with session_factory()() as changed:
            if change == "binding":
                changed.get(SourceBinding, state.binding.id).entry_url = "https://www.zhipin.com/gongsi/changed.html"
            else:
                task = changed.get(TaskRun, state.task.id)
                if change == "cancel":
                    task.cancel_requested = True
                else:
                    task.started_at = now() - timedelta(seconds=get_settings().task_timeout_seconds + 1)
            changed.commit()
    state.runner.on_get = alter
    with pytest.raises(CollectionError) as error:
        adapter.invoke(state.context)
    assert error.value.code == expected
    stored = execution(db)
    assert stored.status != "accepted" and stored.error_code == expected
    assert not adapter.evidence()["verified"]


@pytest.mark.parametrize("failure,expected", [("truth", "adapter_program_invalid"), ("environment", "runner_dependency_environment_changed")])
def test_signed_but_false_or_wrong_environment_output_is_rejected_and_audited(db, failure, expected):
    state = seed_runtime(db)
    if failure == "truth":
        state.runner.output["records"][0]["fields"]["title"]["value"] = "凭空编造的职位"
    else:
        state.runner.environment = "d" * 64
    adapter = runtime(state)
    with pytest.raises(CollectionError) as error:
        adapter.invoke(state.context)
    assert error.value.code == expected
    stored = execution(db)
    assert stored.status == "failed" and stored.error_code == expected and stored.result["status"] == "succeeded"
    assert not adapter.evidence()["verified"]
    with pytest.raises(CollectionError):
        runtime(state).invoke(state.context)
    assert len(state.runner.posts) == 1 and len(state.runner.gets) == 1


@pytest.mark.parametrize("status,error_code,expected", [
    ("unknown", "broker_restarted", "runner_execution_unknown"),
    ("cancelled", "cancelled", "runner_execution_cancelled"),
    ("failed", "docker_unavailable", "runner_infrastructure_failed"),
])
def test_signed_remote_non_success_is_terminal_and_not_resubmitted(db, status, error_code, expected):
    state = seed_runtime(db)
    state.runner.status, state.runner.error_code = status, error_code
    adapter = runtime(state)
    with pytest.raises(CollectionError) as error:
        adapter.invoke(state.context)
    assert error.value.code == expected
    stored = execution(db)
    assert stored.status == status and stored.error_code == expected and stored.result["error_code"] == error_code
    with pytest.raises(CollectionError):
        runtime(state).invoke(state.context)
    assert len(state.runner.posts) == 1 and len(state.runner.gets) == 1
    assert not adapter.evidence()["verified"]


def test_cleanup_pending_refreshes_same_remote_result_instead_of_reusing_unconfirmed_cache(db):
    state = seed_runtime(db)
    state.runner.cleanup = False
    with pytest.raises(CollectionError) as error:
        runtime(state).invoke(state.context)
    assert error.value.code == "runner_cleanup_pending"
    state.runner.cleanup = True
    assert runtime(state).invoke(state.context).records
    assert len(state.runner.posts) == 1 and len(state.runner.gets) == 2


def test_wait_budget_has_no_infinite_events_or_hidden_model_calls(db):
    state = seed_runtime(db)
    state.runner.pending = True
    before = db.scalar(select(func.count()).select_from(ModelCall))
    with pytest.raises(CollectionError) as error:
        runtime(state, wait_seconds=0.03, poll_seconds=0.01).invoke(state.context)
    assert error.value.code == "runner_wait_timeout"
    assert len(state.runner.gets) <= 5
    assert execution(db).error_code == "runner_wait_timeout"
    assert db.scalar(select(func.count()).select_from(TaskEvent)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == before


def test_unrelated_binding_cannot_be_added_to_frozen_task_scope(db):
    state = seed_runtime(db)
    other = SourceBinding(company_id=state.binding.company_id, kind="boss", entry_url="https://www.zhipin.com/gongsi/other.html")
    db.add(other)
    db.commit()
    with pytest.raises(CollectionError) as error:
        freeze_for_task("boss", task_id=state.task.id, binding_id=other.id, expected_attempt=1)
    assert error.value.code == "code_runtime_scope"


def test_navigation_acceptance_uses_observed_link_evidence(db):
    state = seed_runtime(db)
    context = prepare_program_input("boss", state.snapshot, program_support.IDENTITY, operation="navigation",
                                    page_url=program_support.PAGE, html=program_support.HTML)
    link = next(item["id"] for item in context.request["links"] if item["purpose"] == "next")
    state.runner.output = program_support.output(context, navigation=[{"type": "follow", "link_id": link}])
    adapter = runtime(state)
    result = adapter.invoke(context)
    assert result[0].url == "https://www.zhipin.com/web/geek/job?page=2"
    assert adapter.evidence()["verified"]


def test_detail_acceptance_uses_first_party_job_and_exact_description(db):
    state = seed_runtime(db)
    job = validate_list_output(state.context, state.output).records[0]
    description = "岗位职责：负责企业平台研发与数据处理流程，参与接口开发和性能优化。"
    context = prepare_program_input("boss", state.snapshot, program_support.IDENTITY, operation="parse_detail",
                                    page_url=job.url, job=job,
                                    html='<article class="job-detail"><p class="description">' + description + '</p></article>')
    state.runner.output = program_support.output(context, row={"document_id": "dom", "selector": ".job-detail"},
        fields={"description": program_support.claim(description, selector=".description")})
    adapter = runtime(state)
    result, navigation = adapter.invoke(context)
    assert result.source_job_id == job.source_job_id and result.description == description and navigation == []
    assert adapter.evidence()["verified"] and execution(db).operation == "parse_detail"


def test_forged_receipt_is_saved_for_audit_but_never_accepted(db):
    state = seed_runtime(db)
    original = state.runner.get_run
    def corrupted(run_id):
        result = original(run_id)
        result["receipt"]["signature"] = "0" * 64
        return result
    state.runner.get_run = corrupted
    adapter = runtime(state)
    with pytest.raises(CollectionError) as error:
        adapter.invoke(state.context)
    assert error.value.code == "runner_receipt_invalid"
    stored = execution(db)
    assert stored.status == "failed" and stored.error_code == "runner_receipt_invalid"
    assert stored.result["receipt"]["signature"] == "0" * 64 and not adapter.evidence()["verified"]


def test_official_checkpoint_authorizes_only_its_own_config_transition(db):
    state = seed_runtime(db, kind="official")
    checkpoint = OfficialCheckpointStore.open(state.task.id, state.snapshot, state.task.config_snapshot,
                                               state.source.id, 1)
    checkpoint.remember_plan({"mode": "css", "list_selector": ".job-list", "job_link_selector": "a.job"},
                              uuid.uuid4(), "fixture-no-model")
    with pytest.raises(CollectionError) as strict:
        runtime(state)
    assert strict.value.code == "source_changed"
    adapter = runtime(state, scope_guard=checkpoint.guard_in_session)
    assert adapter.invoke(state.context)[0].url == "https://careers.example/jobs?page=2"
    assert adapter.evidence()["verified"] and state.descriptor.kind == "official"
    with session_factory()() as changed:
        binding = changed.get(SourceBinding, state.binding.id)
        binding.config = {**binding.config, "unapproved_change": True}
        changed.commit()
    with pytest.raises(CollectionError) as rejected:
        adapter.invoke(state.context)
    assert rejected.value.code == "checkpoint_scope_changed"
    assert len(state.runner.posts) == 1


def test_scope_guard_cannot_override_account_or_domain_changes(db):
    state = seed_runtime(db)
    called = []
    adapter = runtime(state, scope_guard=lambda db: called.append(True))
    called.clear()
    with session_factory()() as changed:
        changed.get(SourceBinding, state.binding.id).entry_url = "https://other.example/jobs"
        changed.commit()
    with pytest.raises(CollectionError) as rejected:
        adapter.invoke(state.context)
    assert rejected.value.code == "source_changed" and called == [] and state.runner.posts == []
