"""Real PostgreSQL orchestration with mock model/runner; candidate source is never executed."""
from copy import deepcopy
import hashlib
import hmac
import json

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError

from app.adapter_code_models import AdapterCodeProposal
from app.adapter_code_orchestration import (
    code_tick_due, finish_code_recovery, on_code_published, prepare_code_recovery,
    queue_code_from_failure, queue_code_from_upstream, reconcile_code_recoveries, record_code_failure, tick_code_maintenance,
)
from app.adapter_code_package import canonical_bytes, content_sha256
from app.adapter_code_release import publish_code_proposal
from app.adapter_maintenance import ensure_registry
from app.adapter_runtime_models import AdapterProgramExecution
from app.collection import CollectionError, CollectionResult, JobDocument
from app.db import session_factory
from app.ingest import ingest
from app.maintenance_models import AdapterMaintenanceRun, AdapterRegistry, AdapterRelease
from app.models import Company, Job, ModelCall, SourceAccount, SourceBinding, SourceRun, Subject, TaskRun, User
from app.schemas import RunInput
from app.tasks import create_batch
from test_adapter_code_service import SignedOutcomeRunner, gateway


TEXT = "岗位职责：负责平台研发与系统架构设计，维护稳定的数据处理流程，参与接口开发和性能优化，与产品团队协作完成迭代。任职要求：熟悉数据库和软件工程。"
PATCH = {"edits": [{"path": "adapter.py", "old": 'selectors = RULES["detail_selectors"]',
                    "new": 'selectors = [*RULES["detail_selectors"], ".jd-v2"]'}]}


class OracleRunner(SignedOutcomeRunner):
    """Signed transport fixture only; it returns frozen server oracles, never runs code."""
    def __init__(self, cases):
        super().__init__("succeeded", None)
        self.cases = deepcopy(cases)

    def resolve_dependencies(self, dependencies):
        lock = {"schema_version": 1, "python": "3.12", "platform": "linux/amd64", "dependencies": dependencies,
                "wheels": [{"name": pin.split("==")[0], "version": pin.split("==")[1],
                            "filename": pin.split("==")[0] + "-" + pin.split("==")[1] + "-py3-none-any.whl", "sha256": "d" * 64}
                           for pin in dependencies], "resolver_policy": "approved-pure-wheels-v1"}
        self.lock_sha = content_sha256(lock)
        return {"lock": lock, "lock_sha256": self.lock_sha}

    def get_run(self, run_id):
        result = super().get_run(run_id)
        request = next(item["request"] for item in self.runs.values() if item["run_id"] == run_id)
        output = next(case["expected"] for case in self.cases if case["input"] == request["input"] and case["operation"] == request["operation"])
        result["output"] = deepcopy(output)
        receipt = {key: value for key, value in result["receipt"].items() if key != "signature"}
        receipt.update(output_sha256=content_sha256(output), exit_code=0)
        key = hmac.new(self.token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
        receipt["signature"] = hmac.new(key, canonical_bytes(receipt), hashlib.sha256).hexdigest()
        result["receipt"] = receipt
        return result


def seed(db, *, auto=False):
    subject = Subject(name="甲公司", aliases=["甲集团"])
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name=subject.name)
    account = SourceAccount(kind="boss", name="测试专用账号", health="saved")
    db.add_all([company, account])
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="boss", account_id=account.id, entry_url="https://www.zhipin.com/gongsi/acme.html")
    db.add(binding)
    db.flush()
    registry = ensure_registry(db, "boss")
    registry.code_agent_enabled, registry.code_auto_publish_enabled = True, auto
    registry.agent_enabled = registry.auto_repair_enabled = False
    db.commit()
    batch, _ = create_batch(db, RunInput(company_ids=[company.id]))
    task = db.scalar(select(TaskRun).where(TaskRun.batch_id == batch.id))
    task.status, task.attempt = "failed", 1
    task.config_snapshot = {**task.config_snapshot, "adapter_versions": {str(binding.id): {"fingerprint": "old", "release_id": str(registry.active_release_id)}}}
    source = SourceRun(task_id=task.id, binding_id=binding.id, scope_hash=task.scope_hash, status="failed", error_code="source_contract_changed")
    db.add(source)
    db.flush()
    release = db.get(AdapterRelease, registry.active_release_id)
    evidence = {"type": "detail_dom", "kind": "boss", "html": '<div class="jd-v2">' + TEXT + '</div>',
                "expected_text_hash": hashlib.sha256(TEXT.encode()).hexdigest(), "expected": {"title": "不可作为真值", "source_job_id": "forged"}}
    incident = record_code_failure(db, task.id, source.id, task.config_snapshot["sources_snapshot"][0],
                                  "source_contract_changed", evidence, release.fingerprint)
    db.commit()
    return incident, registry, company, binding, subject


def proposal_for(db, *, auto=False):
    incident, registry, company, binding, subject = seed(db, auto=auto)
    proposal = queue_code_from_failure(db, incident.id)
    assert proposal is not None, incident.details
    db.commit()
    return proposal, incident, registry, company, binding, subject


def advance_to_waiting(db):
    proposal, incident, registry, company, binding, subject = proposal_for(db)
    runner, calls = OracleRunner(proposal.evidence_cases), []
    model = gateway(PATCH, calls)
    for _ in range(5):
        tick_code_maintenance(proposal.id, client=runner, gateway=model)
    db.expire_all()
    assert db.get(AdapterCodeProposal, proposal.id).status == "awaiting_publish"
    return proposal, incident, registry, company, binding, subject, runner, calls


def test_real_create_batch_scope_and_server_oracle_ignore_client_expected(db):
    proposal, incident, registry, _, _, _ = proposal_for(db)
    assert not registry.agent_enabled and not registry.auto_repair_enabled
    assert proposal.task_id == incident.maintenance_task_id and proposal.attempts == 0
    assert "不可作为真值" not in json.dumps(proposal.evidence_cases, ensure_ascii=False)
    assert queue_code_from_failure(db, incident.id).id == proposal.id
    assert db.scalar(select(func.count()).select_from(AdapterCodeProposal)) == 1
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("change", ["alias", "adapter", "disabled", "missing_oracle"])
def test_source_identity_changes_or_unprovable_data_do_not_generate(db, change):
    incident, _, _, binding, subject = seed(db)
    if change == "alias":
        subject.aliases = ["另一家公司"]
    elif change == "adapter":
        binding.adapter = "changed"
    elif change == "disabled":
        binding.enabled = False
    else:
        incident.evidence = {"type": "detail_dom", "html": "短文字", "expected": {"description": TEXT}}
    db.commit()
    assert queue_code_from_failure(db, incident.id) is None
    assert incident.status == "needs_engineering"
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("state,attempts", [("completed", 2), ("unknown", 1), ("reserved", 1), ("failed", 1)])
def test_json_and_code_share_two_slots_and_unknown_or_infrastructure_never_regenerates(db, state, attempts):
    incident, _, _, _, _ = seed(db)
    task = TaskRun(domain="maintenance", kind="automatic_adapter_repair", scope_hash="test", status="failed")
    db.add(task)
    db.flush()
    incident.maintenance_task_id, incident.attempts = task.id, attempts
    db.add(ModelCall(task_id=task.id, purpose="automatic_adapter_repair", model="mock", status=state))
    db.commit()
    assert queue_code_from_failure(db, incident.id) is None
    assert db.scalar(select(func.count()).select_from(AdapterCodeProposal)) == 0


def test_one_consumed_json_slot_leaves_only_one_code_candidate(db):
    incident, _, _, _, _ = seed(db)
    task = TaskRun(domain="maintenance", kind="automatic_adapter_repair", scope_hash="test", status="failed")
    db.add(task)
    db.flush()
    incident.maintenance_task_id, incident.attempts = task.id, 1
    db.add(ModelCall(task_id=task.id, purpose="automatic_adapter_repair", model="mock", status="completed"))
    db.commit()
    proposal = queue_code_from_failure(db, incident.id)
    db.commit()
    assert proposal.attempts == 1 and proposal.max_candidates == 2 and proposal.task_id == task.id
    calls = []
    tick_code_maintenance(proposal.id, client=OracleRunner(proposal.evidence_cases), gateway=gateway({"edits": []}, calls))
    db.expire_all()
    assert len(calls) == 1 and db.get(AdapterCodeProposal, proposal.id).attempts == 2
    tick_code_maintenance(proposal.id, client=OracleRunner(proposal.evidence_cases), gateway=gateway(PATCH, calls))
    assert len(calls) == 1


def test_automatic_publish_builds_exactly_one_new_source_recovery_and_drops_old_freeze(db):
    proposal, incident, registry, _, binding, _, runner, calls = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    tick_code_maintenance(proposal.id, client=runner)
    db.expire_all()
    incident = db.get(type(incident), incident.id)
    recovery = db.get(TaskRun, incident.recovery_task_id)
    assert recovery and len(recovery.config_snapshot["sources_snapshot"]) == 1
    assert "adapter_versions" not in recovery.config_snapshot
    assert recovery.config_snapshot["sources_snapshot"][0]["id"] == str(binding.id)
    assert recovery.config_snapshot["sources_snapshot"][0]["expected_release_generation"] == 1
    assert db.get(AdapterCodeProposal, proposal.id).status == "recovery_queued"
    assert not code_tick_due(db.get(AdapterCodeProposal, proposal.id), registry)
    on_code_published(db, db.get(AdapterCodeProposal, proposal.id), db.get(AdapterRelease, incident.release_id))
    db.commit()
    assert len(calls) == 1 and incident.recovery_attempts == 1
    recovery.status, recovery.attempt = "running", 1
    db.commit()
    prepare_code_recovery(db, recovery.id)
    db.commit()
    with pytest.raises(CollectionError):
        prepare_code_recovery(db, recovery.id)


def test_explicit_admin_publish_authorizes_recovery_with_auto_publish_off(db):
    proposal, incident, registry, _, _, _, runner, _ = advance_to_waiting(db)
    admin = User(username="code-publisher", password_hash="unused", role="admin", active=True)
    db.add(admin)
    db.commit()
    publish_code_proposal(db, proposal.id, actor_id=admin.id, expected_active_release_id=proposal.base_release_id,
                          expected_generation=proposal.base_generation, client=runner)
    db.commit()
    db.refresh(incident)
    recovery = db.get(TaskRun, incident.recovery_task_id)
    assert recovery.config_snapshot["code_manual_publish_actor_id"] == str(admin.id)
    assert not registry.code_auto_publish_enabled
    recovery.status, recovery.attempt = "running", 1
    db.commit()
    prepare_code_recovery(db, recovery.id)
    db.commit()
    assert incident.status == "code_recovering"


def test_website_change_before_publication_rolls_back_the_entire_publish(db):
    proposal, _, registry, _, binding, _, runner, _ = advance_to_waiting(db)
    previous = registry.active_release_id
    binding.entry_url = "https://www.zhipin.com/gongsi/another.html"
    registry.code_auto_publish_enabled = True
    db.commit()
    outcome = tick_code_maintenance(proposal.id, client=runner)
    assert outcome["status"] == "failed"
    db.refresh(registry)
    assert registry.active_release_id == previous


def test_false_runtime_counts_are_not_successful_recovery(db):
    proposal, incident, registry, _, binding, _, runner, _ = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    tick_code_maintenance(proposal.id, client=runner)
    db.expire_all()
    incident = db.get(type(incident), incident.id)
    recovery = db.get(TaskRun, incident.recovery_task_id)
    recovery.status, recovery.attempt = "partial", 1
    release = db.get(AdapterRelease, incident.release_id)
    proof = {"release_id": str(release.id), "release_generation": 1, "fingerprint": release.fingerprint,
             **{key: value for key, value in release.manifest["program"].items() if key != "contract"}, "verified": True, "run_ids": ["fake-run"]}
    db.add(SourceRun(task_id=recovery.id, binding_id=binding.id, scope_hash=recovery.scope_hash, status="partial", counts={"observed": 1, "code_execution": proof}))
    db.commit()
    finish_code_recovery(db, recovery.id, client=runner)
    db.commit()
    assert incident.status == "failed"


def test_upstream_check_samples_once_and_reuses_same_target_event(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.code_agent_enabled = registry.code_upstream_enabled = True
    base = registry.upstream_commit
    target = "a" * 40
    check = AdapterMaintenanceRun(registry_id=registry.id, kind="upstream_check", status="needs_engineering", upstream_commit=target)
    db.add(check)
    db.commit()
    calls = []
    def sample(kind, old, new, **kwargs):
        calls.append((kind, old, new))
        return {"repository": "https://github.com/eatmoreduck/boss-zhipin-scraper", "base_commit": old, "target_commit": new,
                "files": [{"path": "scripts/boss_cdp_raw.py", "diff": "-old\n+new", "status": "modified"}], "snapshot_sha256": "f" * 64}
    monkeypatch.setattr("app.adapter_code_orchestration.sample_upstream_diff", sample)
    proposal = queue_code_from_upstream(db, check.id)
    db.commit()
    assert proposal and proposal.origin == "fixed_upstream" and calls == [("boss", base, target)]
    another = AdapterMaintenanceRun(registry_id=registry.id, kind="upstream_check", status="needs_engineering", upstream_commit=target)
    db.add(another)
    db.commit()
    assert queue_code_from_upstream(db, another.id).id == proposal.id
    assert len(calls) == 1 and db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("terminal", ["cancelled", "failed", "waiting_login", "waiting_verification"])
def test_reconcile_settles_before_worker_claim_without_new_model_or_runner(db, terminal):
    proposal, incident, registry, _, _, _, runner, calls = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    tick_code_maintenance(proposal.id, client=runner)
    db.expire_all()
    recovery = db.get(TaskRun, incident.recovery_task_id)
    assert recovery.attempt == 0
    recovery.status, recovery.cancel_requested = terminal, False
    db.commit()
    previous = len(runner.submissions)
    reconcile_code_recoveries(db)
    db.commit()
    assert incident.status == ("cancelled" if terminal == "cancelled" else "failed")
    assert len(runner.submissions) == previous and len(calls) == 1
    reconcile_code_recoveries(db)
    db.commit()
    assert incident.recovery_attempts == 1


@pytest.mark.parametrize("observed", [True, False])
def test_recovery_requires_signed_task_scoped_ledger_and_real_ingested_observation(db, observed):
    proposal, incident, registry, _, binding, _, runner, _ = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    tick_code_maintenance(proposal.id, client=runner)
    db.expire_all()
    recovery = db.get(TaskRun, incident.recovery_task_id)
    recovery.status, recovery.attempt = "partial", 1
    release = db.get(AdapterRelease, incident.release_id)
    program = release.manifest["program"]
    source = SourceRun(task_id=recovery.id, binding_id=binding.id, scope_hash=recovery.scope_hash, status="partial")
    db.add(source)
    db.flush()
    run_ids = []
    for operation in ["parse_list", "parse_detail"]:
        case = next(case for case in proposal.evidence_cases if case["operation"] == operation)
        key = content_sha256({"recovery": str(recovery.id), "operation": operation})
        submitted = runner.submit_run(program["bundle_sha256"], operation, case["input"], idempotency_key=key)
        result = runner.get_run(submitted["run_id"])
        request = runner.runs[key]["request"]
        runner.verify_result(result, request=request, lock_sha256=program["lock_sha256"], expected_runtime=program["runtime_sha256"])
        db.add(AdapterProgramExecution(task_id=recovery.id, source_run_id=source.id, binding_id=binding.id, owner_attempt=1,
            execution_key=key, adapter_fingerprint=release.fingerprint, bundle_sha=program["bundle_sha256"], operation=operation,
            input_sha=content_sha256(request["input"]), request_sha=content_sha256(request), request=request,
            remote_run_id=result["run_id"], status="accepted", result=result))
        run_ids.append(result["run_id"])
    if observed:
        ingest(db, recovery, binding, source, CollectionResult(records=[JobDocument(source_job_id="job1", title="研发工程师",
            url="https://www.zhipin.com/job_detail/job1.html", description=TEXT)], coverage="partial"))
    proof = {"release_id": str(release.id), "release_generation": registry.release_generation, "fingerprint": release.fingerprint,
             **{key: value for key, value in program.items() if key != "contract"}, "verified": True, "run_ids": run_ids}
    source.counts = {**source.counts, "observed": 1, "code_execution": proof}
    db.commit()
    finish_code_recovery(db, recovery.id, client=runner)
    db.commit()
    assert incident.status == ("recovered" if observed else "failed")
    assert db.scalar(select(func.count()).select_from(Job)) == int(observed)


@pytest.mark.parametrize("change", ["task", "binding", "company", "subject"])
def test_publication_holds_scope_rows_until_commit_in_two_sessions(db, change):
    proposal, incident, registry, company, binding, subject, runner, _ = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    publish_code_proposal(db, proposal.id, expected_active_release_id=proposal.base_release_id,
                         expected_generation=proposal.base_generation, client=runner)
    model, identifier, values = {
        "task": (TaskRun, incident.task_id, {"cancel_requested": True}),
        "binding": (SourceBinding, binding.id, {"enabled": False}),
        "company": (Company, company.id, {"archived": True}),
        "subject": (Subject, subject.id, {"aliases": ["新别名"]}),
    }[change]
    statement = update(model).where(model.id == identifier).values(**values)
    with session_factory()() as other:
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError) as exc:
            other.execute(statement)
        assert exc.value.orig.sqlstate == "55P03"
        other.rollback()
        db.commit()
        other.execute(statement)
        other.commit()


@pytest.mark.parametrize("change", ["task", "binding"])
def test_uncommitted_scope_change_cannot_pass_publication_or_deadlock(db, change):
    proposal, incident, registry, _, binding, _, runner, _ = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    previous = registry.active_release_id
    with session_factory()() as other:
        model, identifier, values = (TaskRun, incident.task_id, {"cancel_requested": True}) if change == "task" else (SourceBinding, binding.id, {"enabled": False})
        other.execute(update(model).where(model.id == identifier).values(**values))
        with pytest.raises(CollectionError) as exc:
            publish_code_proposal(db, proposal.id, expected_active_release_id=proposal.base_release_id,
                                 expected_generation=proposal.base_generation, client=runner)
        assert exc.value.code == "source_configuration_busy"
        db.rollback()
        other.commit()
    assert db.get(AdapterRegistry, registry.id, populate_existing=True).active_release_id == previous


def test_busy_publication_retries_same_validated_candidate_without_another_model(db):
    proposal, incident, registry, _, binding, _, runner, calls = advance_to_waiting(db)
    registry.code_auto_publish_enabled = True
    db.commit()
    with session_factory()() as other:
        other.execute(select(SourceBinding).where(SourceBinding.id == binding.id).with_for_update())
        outcome = tick_code_maintenance(proposal.id, client=runner)
        assert outcome["status"] == "awaiting_publish" and outcome["error_code"] == "source_configuration_busy"
        other.rollback()
    outcome = tick_code_maintenance(proposal.id, client=runner)
    assert outcome["status"] == "recovery_queued" and len(calls) == 1
    db.refresh(incident)
    assert incident.recovery_attempts == 1
