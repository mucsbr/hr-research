import json
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.adapter_code_models import AdapterCodePackage
from app.adapter_code_package import canonical_bytes, content_sha256, load_bundle
from app.adapter_runner_client import AdapterRunnerClient
from app.adapter_code_service import (
    advance_code_validation, cancel_code_proposal, compare_oracle, create_code_proposal,
    freeze_evidence_cases, generate_code_candidate, materialize_model_candidate,
    proposal_messages, reconcile_code_proposal, validate_draft_spec,
)
from app.adapter_maintenance import ensure_registry
from app.collection import CollectionError
from app.config import get_settings
from app.llm import ModelGateway
from app.models import ModelCall, TaskRun


def spec(source='def run(operation, payload):\n    return None\n', dependencies=None):
    return {"schema_version": 1, "contract": "python_adapter_v1", "kind": "boss",
            "files": {"adapter.py": source}, "dependencies": dependencies or []}


def cases():
    return [{"id": "list", "operation": "parse_list", "input": {"jobs": [{"title": "工程师"}]},
             "expected": {"jobs": [{"title": "工程师"}]}, "role": "failure"},
            {"id": "detail", "operation": "parse_detail", "input": {"html": "<p>负责研发</p>"},
             "expected": {"description": "负责研发"}},
            {"id": "navigation", "operation": "navigation", "input": {"links": ["https://www.zhipin.com/job_detail/1.html"]},
             "expected": {"urls": ["https://www.zhipin.com/job_detail/1.html"]}}]


def candidate():
    return spec('def run(operation, payload):\n    return {"changed": payload}\n')


class FakeRunner:
    """Protocol fixture only: no eval/import/exec and no network or candidate execution."""
    def __init__(self, bad_output=False, lose_first_response=False):
        self.runs, self.submissions = {}, []
        self.bad_output, self.lose_first_response = bad_output, lose_first_response
        self.lookup_calls = 0
        self.health_calls = 0
        self.cancelled = []

    def health(self):
        self.health_calls += 1
        return {"status": "ok", "contract": "python_adapter_v1", "runtime_sha256": "sha256:" + "a" * 64}

    def resolve_dependencies(self, dependencies):
        lock = {"schema_version": 1, "python": "3.12", "platform": "linux/amd64",
                "dependencies": dependencies, "wheels": [], "resolver_policy": "approved-pure-wheels-v1"}
        return {"lock": lock, "lock_sha256": content_sha256(lock)}

    def upload_bundle(self, bundle):
        return {"bundle_sha256": content_sha256(bundle), "lock_sha256": content_sha256(bundle["lock"]), "created": True}

    def submit_run(self, bundle_sha, operation, payload, *, idempotency_key):
        request = {"bundle_sha256": bundle_sha, "operation": operation, "input": payload, "idempotency_key": idempotency_key}
        self.submissions.append(request)
        run = {"run_id": "fixture-" + idempotency_key, "status": "succeeded", "request_sha256": content_sha256(request),
               "operation": operation, "request": request}
        self.runs[idempotency_key] = run
        if self.lose_first_response:
            self.lose_first_response = False
            raise CollectionError("runner_unknown", "已提交但响应未知")
        return run

    def lookup_run(self, key):
        self.lookup_calls += 1
        return self.runs.get(key)

    def get_run(self, run_id):
        return next(run for run in self.runs.values() if run["run_id"] == run_id)

    def verify_result(self, result, *, request, lock_sha256, expected_runtime=None):
        assert result["request_sha256"] == content_sha256(request)
        assert expected_runtime == "sha256:" + "a" * 64
        if self.bad_output:
            return {"passed": True}
        return next(case["expected"] for case in cases() if case["operation"] == request["operation"])

    def cancel_run(self, run_id):
        self.cancelled.append(run_id)
        return {"run_id": run_id, "status": "cancelled"}


class SignedOutcomeRunner(FakeRunner):
    """No remote code: real client verifies protocol receipts emitted by this test fixture."""
    def __init__(self, status, error_code, *, execution_started=True, cleanup=True, corrupt_signature=False):
        super().__init__()
        self.outcome, self.error_code = status, error_code
        self.execution_started, self.cleanup, self.corrupt_signature = execution_started, cleanup, corrupt_signature
        self.token = "code-service-receipt-test-only-" + "z" * 40
        settings = SimpleNamespace(adapter_runner_url="https://runner.example.invalid",
                                   adapter_runner_token=SecretStr(self.token), app_env="test",
                                   adapter_runner_runtime_digest="sha256:" + "a" * 64)
        self.verifier = AdapterRunnerClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    def resolve_dependencies(self, dependencies):
        result = super().resolve_dependencies(dependencies)
        self.lock_sha = result["lock_sha256"]
        return result

    def get_run(self, run_id):
        stored = super().get_run(run_id)
        request = stored["request"]
        limits = {"network": "none", "read_only": True, "user": "65532:65532", "cap_drop": ["ALL"],
                  "no_new_privileges": True, "pids_limit": 32, "memory_bytes": 268435456,
                  "cpus": 1, "timeout_seconds": 30}
        result = {"run_id": run_id, "status": self.outcome, "error_code": self.error_code, "output": None}
        receipt = {**result, "operation": request["operation"], "request_sha256": content_sha256(request),
                   "input_sha256": content_sha256(request["input"]), "bundle_sha256": request["bundle_sha256"],
                   "lock_sha256": self.lock_sha, "output_sha256": content_sha256(None),
                   "runtime_sha256": "sha256:" + "a" * 64,
                   "dependency_environment_sha256": "b" * 64, "dependency_installation_sha256": "c" * 64,
                   "cleanup": {"container_removed": self.cleanup, "process_group_terminated": self.cleanup},
                   "execution_started": self.execution_started, "limits": limits, "actual_limits": limits,
                   "exit_code": 1, "oom_killed": False, "signature_algorithm": "hmac-sha256-v1"}
        key = hmac.new(self.token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
        receipt["signature"] = hmac.new(key, canonical_bytes(receipt), hashlib.sha256).hexdigest()
        if self.corrupt_signature:
            receipt["signature"] = "0" * 64
        result["receipt"] = receipt
        return result

    def verify_result(self, result, *, request, lock_sha256, expected_runtime=None):
        return self.verifier.verify_result(result, request=request, lock_sha256=lock_sha256,
                                           expected_runtime=expected_runtime)


def seed(db, **kwargs):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    registry.code_agent_enabled = True
    db.flush()
    proposal = create_code_proposal(db, "boss", base_spec=spec(), source_snapshot=kwargs.pop("source_snapshot", {"reason": "fixture"}),
                                    evidence_cases=cases(), **kwargs)
    db.commit()
    return proposal, registry


def gateway(payload, calls):
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 50, "completion_tokens": 80}})
    return ModelGateway(httpx.MockTransport(respond))


def test_real_source_or_dependency_change_required_not_metadata():
    assert validate_draft_spec(candidate(), base_spec=spec())
    assert validate_draft_spec(spec(dependencies=["packaging==25.0"]), base_spec=spec())
    for source in ["# new comment\n" + spec()["files"]["adapter.py"], '__version__ = "2"\n' + spec()["files"]["adapter.py"]]:
        with pytest.raises(CollectionError) as failed:
            validate_draft_spec(spec(source), base_spec=spec())
        assert failed.value.code == "candidate_no_change"


def test_oracle_requires_three_operations_and_ignores_self_reported_pass():
    with pytest.raises(CollectionError):
        freeze_evidence_cases(cases()[:2])
    assert not compare_oracle(cases()[0], {"passed": True})["passed"]
    assert compare_oracle(cases()[0], cases()[0]["expected"])["passed"]
    assert not compare_oracle({"id": "types", "operation": "parse_list", "input": {}, "expected": 1}, True)["passed"]


def test_source_is_never_imported_or_executed_locally(tmp_path):
    marker = tmp_path / "candidate-must-not-run"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('wrong')\ndef run(operation, payload):\n    return payload\n"
    validate_draft_spec(spec(source), base_spec=spec())
    assert not marker.exists()


def test_literal_incremental_edits_and_new_lib_files_are_validated_without_execution():
    patched = materialize_model_candidate({"edits": [{"path": "adapter.py", "old": "return None", "new": "return payload"}],
                                          "new_files": {"lib/extra.py": "def normalize(value):\n    return str(value)\n"}}, base_spec=spec())
    assert "return payload" in patched["files"]["adapter.py"] and "lib/extra.py" in patched["files"]
    assert patched["dependencies"] == []
    for patch in [{"edits": [{"path": "adapter.py", "old": "does not exist", "new": "x"}]},
                  {"edits": [], "new_files": {"../escape.py": "x=1"}},
                  {"edits": [{"path": "adapter.py", "old": "", "new": "x"}]}]:
        with pytest.raises(CollectionError):
            materialize_model_candidate(patch, base_spec=spec())
    with pytest.raises(CollectionError) as failure:
        materialize_model_candidate({"edits": [{"path": "adapter.py", "old": "None", "new": "payload"}]},
                                    base_spec=spec("def run(operation, payload):\n    x = None\n    return None\n"))
    assert failure.value.code == "candidate_patch_ambiguous"


def test_prompt_sampling_fits_budget_and_records_omitted_context():
    large = spec('HEADER = "' + "x" * 18000 + '"\ndef run(operation, payload):\n    return None\n')
    proposal = SimpleNamespace(base_spec=large, source_snapshot={"files": [{"path": "parser.py", "status": "modified",
                               "diff": "+changed\n" * 5000}]}, evidence_cases=cases(), error_code=None)
    messages, metadata = proposal_messages(proposal, max_input_chars=4000, return_metadata=True)
    assert sum(len(message["content"]) for message in messages) <= 4000
    assert metadata["omitted"] and metadata["selected"]
    body = json.loads(messages[1]["content"])
    assert body["file_excerpts"] and "return None" in body["file_excerpts"][0]["text"]
    assert any(item["type"] == "upstream_diff_tail" for item in metadata["omitted"])


def production_detail_proposal(description_chars=3000):
    from app.adapter_code_catalog import catalog_for, failure_case
    from app.adapter_maintenance import builtin_manifest
    from app.adapter_program import capture_program_failure, prepare_program_input
    from app.collection import JobDocument
    manifest = builtin_manifest("boss")
    catalog = catalog_for("boss", manifest)
    binding = {"kind": "boss", "entry_url": "https://www.zhipin.com/gongsi/offline-audit.html",
               "external_company_id": "offline-audit"}
    identity = {"name": "离线提示审阅公司"}
    sentence = "岗位职责：负责数据平台接口开发、任务调度与岗位历史核验。任职要求：熟悉Python、SQL及软件工程，能够依据公开原文排查字段质量问题。"
    description = (sentence * (description_chars // len(sentence) + 1))[:description_chars]
    html = '<html><body><h1>数据平台工程师</h1><div class="jd-live-qa">' + description + '</div></body></html>'
    job = JobDocument(source_job_id="offlineqa-job", title="数据平台工程师",
                      url="https://www.zhipin.com/job_detail/offlineqa-job.html")
    context = prepare_program_input("boss", binding, identity, operation="parse_detail", page_url=job.url, html=html, job=job)
    failure = failure_case("boss", binding, identity, capture_program_failure(context), manifest)
    return SimpleNamespace(base_spec=catalog["base_spec"], source_snapshot={},
                           evidence_cases=freeze_evidence_cases([*catalog["evidence_cases"], failure]), error_code=None)


@pytest.mark.parametrize("large_upstream_diff", [False, True])
def test_production_detail_prompt_keeps_complete_fault_implementation_and_audits_omissions(large_upstream_diff):
    proposal = production_detail_proposal()
    if large_upstream_diff:
        proposal.source_snapshot = {"files": [{"path": "adapter.py", "status": "modified", "diff": "+upstream_change\n" * 4000}]}
    messages, metadata = proposal_messages(proposal, max_input_chars=16000, return_metadata=True)
    body = json.loads(messages[1]["content"])
    source = "\n".join(entry["text"] for entry in body["file_excerpts"] if entry["path"] == "adapter.py")
    assert 'selectors = RULES["detail_selectors"]' in source
    assert 'nodes = soup.select(selector, limit=2)' in source
    assert 'raise ValueError("No unique verified job description region")' in source
    assert body["failure_cases"] == [proposal.evidence_cases[-1]]
    assert sum(len(message["content"]) for message in messages) == metadata["input_chars"] <= 16000
    assert any(entry["operation"] == "parse_detail" and entry["function"] == "parse_detail"
               for entry in metadata["required_source_context"])
    for path, original in proposal.base_spec["files"].items():
        lines = original.splitlines(keepends=True)
        covered = set()
        for excerpt in [entry for entry in body["file_excerpts"] if entry["path"] == path]:
            span = set(range(excerpt["start_line"] - 1, excerpt["end_line"]))
            assert not covered & span, "Overlapping source windows must be merged, not duplicated"
            assert excerpt["text"] == "".join(lines[excerpt["start_line"] - 1:excerpt["end_line"]])
            covered.update(span)
        omitted = len(lines) - len(covered)
        audit = [entry for entry in metadata["omitted"] if entry["type"] == "source_lines" and entry["path"] == path]
        assert not omitted or audit and audit[0]["count"] == omitted
    assert any(entry["type"] == "source_lines" and entry["count"] > 0 for entry in metadata["omitted"])


def test_oversized_production_failure_is_rejected_without_truncating_the_evidence():
    proposal = production_detail_proposal(description_chars=5000)
    with pytest.raises(CollectionError) as failure:
        proposal_messages(proposal, max_input_chars=16000)
    assert failure.value.code == "input_budget"


def test_fixed_upstream_without_failure_keeps_bounded_source_and_held_out_cases():
    proposal = production_detail_proposal(description_chars=117)
    proposal.evidence_cases = proposal.evidence_cases[:-1]
    proposal.source_snapshot = {"files": [{"path": "adapter.py", "status": "modified", "diff": "+upstream_change\n" * 4000}]}
    messages, metadata = proposal_messages(proposal, max_input_chars=4000, return_metadata=True)
    body = json.loads(messages[1]["content"])
    assert not body["failure_cases"] and not metadata["required_source_context"]
    assert body["file_excerpts"] and body["upstream"]["files"]
    assert sum(len(message["content"]) for message in messages) <= 4000
    assert any(entry["type"] == "upstream_diff_tail" for entry in metadata["omitted"])
    assert len([entry for entry in metadata["omitted"] if entry.get("reason") == "held_out_regression"]) == 3


def test_optional_head_merges_with_prioritized_fault_window_without_losing_its_tail():
    prefix = "".join(f"CONTEXT_{index} = {('context-marker-' * 4)!r}\n" for index in range(10))
    handler = "def parse_detail(payload):\n" + "".join(
        f"    value_{index} = {('job-marker-' * 5)!r}\n" for index in range(20)) + "    return payload\n"
    source = prefix + handler + 'PADDING = "' + "x" * 20000 + '"\ndef run(operation, payload):\n    return parse_detail(payload)\n'
    evidence = cases()
    evidence[0]["role"], evidence[1]["role"] = "regression", "failure"
    proposal = SimpleNamespace(base_spec=spec(source), source_snapshot={}, evidence_cases=evidence, error_code=None)
    messages, metadata = proposal_messages(proposal, max_input_chars=6000, return_metadata=True)
    body = json.loads(messages[1]["content"])
    beginning = next(entry for entry in body["file_excerpts"] if entry["start_line"] == 1)
    assert beginning["text"].startswith(prefix)
    assert beginning["text"].endswith(handler)
    assert metadata["required_source_context"][0]["end_line"] == beginning["end_line"]
    covered = set()
    for excerpt in body["file_excerpts"]:
        span = set(range(excerpt["start_line"], excerpt["end_line"] + 1))
        assert not covered & span
        covered.update(span)
    assert metadata["input_chars"] <= 6000


def test_fault_function_too_large_rejects_before_paid_request(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.code_agent_enabled = True
    large = spec('def run(operation, payload):\n    text = "' + "x" * 6000 + '"\n    return {"value": text}\n')
    proposal = create_code_proposal(db, "boss", base_spec=large, source_snapshot={}, evidence_cases=cases(), max_candidates=1)
    db.commit()
    monkeypatch.setattr(get_settings(), "llm_max_input_chars", 4000)
    calls, runner = [], FakeRunner()
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "failed"
    db.refresh(proposal)
    assert proposal.error_code == "input_budget" and proposal.attempts == 0
    assert calls == [] and db.scalar(select(func.count()).select_from(ModelCall)) == 0
    assert runner.submissions == []


def test_proposal_dedup_and_runner_missing_costs_zero(db):
    proposal, _ = seed(db)
    again, _ = seed(db)
    assert again.id == proposal.id
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls)) == "blocked_configuration"
    db.refresh(proposal)
    assert proposal.attempts == 0 and calls == []
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_validated_candidate_keeps_active_version_and_protocol_reentry_is_idempotent(db):
    proposal, registry = seed(db)
    previous = registry.active_release_id
    calls, runner = [], FakeRunner()
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "draft_ready"
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "draft_ready"
    assert len(calls) == 1
    assert advance_code_validation(proposal.id, client=runner)["status"] == "resolved"
    assert advance_code_validation(proposal.id, client=runner)["status"] == "validating"
    result = advance_code_validation(proposal.id, client=runner)
    assert result["status"] == "validated" and result["validation"]["passed"]
    assert advance_code_validation(proposal.id, client=runner)["status"] == "validated"
    assert len(runner.submissions) == 3
    db.refresh(registry)
    assert registry.active_release_id == previous
    assert db.scalar(select(func.count()).select_from(AdapterCodePackage)) == 1
    stored = db.scalar(select(AdapterCodePackage))
    assert stored.file_ref.startswith("adapter-code/")
    assert load_bundle(stored.file_ref, expected_sha256=stored.bundle_sha)["files"] == candidate()["files"]


def test_lost_submit_response_queries_same_key_without_new_execution_or_model(db):
    proposal, _ = seed(db)
    calls, runner = [], FakeRunner(lose_first_response=True)
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    interrupted = advance_code_validation(proposal.id, client=runner)
    assert interrupted["status"] == "validating" and interrupted["error_code"] == "runner_unknown"
    assert advance_code_validation(proposal.id, client=runner)["status"] == "validated"
    assert runner.lookup_calls == 1 and len(runner.submissions) == 3 and len(calls) == 1


def test_bad_code_cannot_claim_pass_and_second_candidate_budget_is_durable(db):
    proposal, registry = seed(db)
    old = registry.active_release_id
    calls, runner = [], FakeRunner(bad_output=True)
    for expected in ("retry_ready", "failed"):
        generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
        advance_code_validation(proposal.id, client=runner)
        advance_code_validation(proposal.id, client=runner)
        assert advance_code_validation(proposal.id, client=runner)["status"] == expected
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "failed"
    db.refresh(registry)
    db.refresh(proposal)
    assert registry.active_release_id == old and proposal.attempts == 2 and len(calls) == 2
    assert len([item for item in proposal.candidate_history if item["status"] == "validation_failed"]) == 2


def test_shared_json_and_code_budget_cannot_become_four_calls(db):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    registry.code_agent_enabled = True
    task = TaskRun(domain="maintenance", kind="automatic_adapter_repair", status="failed")
    db.add(task)
    db.flush()
    for _ in range(2):
        db.add(ModelCall(task_id=task.id, purpose="automatic_adapter_repair", model="fixture", input_chars=10, max_output_tokens=10))
    db.flush()
    proposal = create_code_proposal(db, "boss", base_spec=spec(), source_snapshot={}, evidence_cases=cases(),
                                    shared_task_id=task.id, existing_attempts=0)
    db.commit()
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=FakeRunner()) == "failed"
    assert calls == []
    db.refresh(proposal)
    assert proposal.attempts == 2 and proposal.error_code == "candidate_budget"


def test_upstream_docs_skip_without_runner_or_paid_call(db):
    proposal, _ = seed(db, origin="fixed_upstream", source_snapshot={"files": [{"path": "README.md", "diff": "+docs"}]})
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls)) == "skipped"
    assert calls == []


def test_generation_change_and_cancel_prevent_remote_submission(db):
    proposal, registry = seed(db)
    calls, runner = [], FakeRunner()
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    db.refresh(registry)
    registry.release_generation += 1
    db.commit()
    result = advance_code_validation(proposal.id, client=runner)
    assert result["status"] == "conflict" and runner.submissions == []


def test_interrupted_generation_is_unknown_not_repeated(db):
    proposal, _ = seed(db)
    proposal.status, proposal.attempts = "generating", 1
    task = db.get(TaskRun, proposal.task_id)
    task.status = "failed"
    reconcile_code_proposal(db, proposal.id)
    db.commit()
    assert proposal.status == "unknown"
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=FakeRunner()) == "unknown"
    assert calls == []


def test_unhealthy_runner_is_zero_cost_before_candidate_reservation(db):
    proposal, _ = seed(db)
    class Unhealthy(FakeRunner):
        def health(self):
            raise CollectionError("runner_not_ready", "测试运行环境未准备好")
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=Unhealthy()) == "blocked_configuration"
    db.refresh(proposal)
    assert proposal.attempts == 0 and calls == []
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_invalid_model_candidates_stop_at_two_and_keep_old_release(db):
    proposal, registry = seed(db)
    original = registry.active_release_id
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(spec(), calls), client=FakeRunner()) == "failed"
    assert len(calls) == 2
    db.refresh(registry)
    assert registry.active_release_id == original


def test_concurrent_generation_does_not_make_duplicate_paid_requests(db):
    proposal, _ = seed(db)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate())}, "finish_reason": "stop"}]})
    model = ModelGateway(httpx.MockTransport(respond))
    runner = FakeRunner()
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(generate_code_candidate, proposal.id, gateway=model, client=runner)
        try:
            assert entered.wait(5)
            assert generate_code_candidate(proposal.id, gateway=model, client=runner) == "generating"
        finally:
            release.set()
        assert first.result(timeout=5) == "draft_ready"
    assert len(calls) == 1
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 1


def test_cancel_after_unknown_submit_uses_old_key_and_never_restarts(db):
    proposal, _ = seed(db)
    calls, runner = [], FakeRunner(lose_first_response=True)
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    result = cancel_code_proposal(proposal.id, client=runner)
    assert result["status"] == "cancelled" and len(runner.cancelled) == 1
    assert len(runner.submissions) == 1 and runner.lookup_calls == 1
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "cancelled"
    assert len(calls) == 1


def test_stored_package_cannot_be_mutated(db):
    proposal, _ = seed(db)
    runner = FakeRunner()
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), []), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    package = db.scalar(select(AdapterCodePackage))
    package.lock_sha = "b" * 64
    with pytest.raises(ValueError, match="不可修改"):
        db.flush()
    db.rollback()


def test_actual_mock_model_delta_materializes_complete_candidate(db):
    proposal, _ = seed(db)
    delta = {"edits": [{"path": "adapter.py", "old": "return None", "new": "return payload"}]}
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(delta, calls), client=FakeRunner()) == "draft_ready"
    db.refresh(proposal)
    assert proposal.draft_spec["files"]["adapter.py"].endswith("return payload\n")
    task = db.get(TaskRun, proposal.task_id)
    assert len(task.config_snapshot["code_model_contexts"]) == 1
    assert task.config_snapshot["code_model_contexts"][0]["input_chars"] <= get_settings().llm_max_input_chars
    assert len(calls) == 1 and calls[0]["max_tokens"] <= 1200


def test_too_small_context_budget_rejects_before_model_with_zero_calls(db, monkeypatch):
    proposal, _ = seed(db)
    monkeypatch.setattr(get_settings(), "llm_max_input_chars", 100)
    calls = []
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=FakeRunner()) == "failed"
    db.refresh(proposal)
    assert proposal.error_code == "input_budget" and proposal.attempts == 0
    assert calls == [] and db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("remote_status,error,started,expected_status,classification", [
    ("unknown", "broker_restarted", True, "unknown", "runner_execution_unknown"),
    ("cancelled", "cancelled", False, "cancelled", "runner_execution_cancelled"),
    ("failed", "docker_unavailable", False, "failed", "runner_infrastructure_failed"),
    ("failed", "dependency_environment_tampered", True, "failed", "runner_infrastructure_failed"),
])
def test_signed_non_candidate_terminal_stops_without_more_models_or_keys(
        db, remote_status, error, started, expected_status, classification):
    proposal, registry = seed(db)
    previous = registry.active_release_id
    calls, runner = [], SignedOutcomeRunner(remote_status, error, execution_started=started)
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    result = advance_code_validation(proposal.id, client=runner)
    assert result == {"proposal_id": str(proposal.id), "status": expected_status, "error_code": classification}
    db.refresh(proposal)
    first = proposal.validation_run_refs[0]
    assert first["status"] == remote_status and first["verification_outcome"] == classification
    assert first["receipt"] == runner.get_run(first["run_id"])
    assert first["error_code"] == error and first["request"]["idempotency_key"] == runner.submissions[0]["idempotency_key"]
    assert len(runner.submissions) == 1 and len(calls) == 1 and proposal.attempts == 1
    assert advance_code_validation(proposal.id, client=runner)["status"] == expected_status
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == expected_status
    db.refresh(registry)
    assert registry.active_release_id == previous and len(runner.submissions) == 1 and len(calls) == 1


def test_signed_candidate_failure_alone_can_use_remaining_candidate_slot(db):
    proposal, _ = seed(db)
    calls, runner = [], SignedOutcomeRunner("failed", "candidate_exit_nonzero")
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    assert advance_code_validation(proposal.id, client=runner)["status"] == "retry_ready"
    db.refresh(proposal)
    history = proposal.candidate_history[-1]
    assert history["status"] == "validation_failed"
    first = history["validation_runs"][0]
    assert first["receipt"]["error_code"] == "candidate_exit_nonzero"
    assert first["receipt"]["receipt"]["error_code"] == "candidate_exit_nonzero"
    assert first["verification_outcome"] == "runner_execution_failed"
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "draft_ready"
    assert len(calls) == 2


def test_unconfirmed_cleanup_waits_on_original_run_then_accepts_signed_unknown(db):
    proposal, _ = seed(db)
    calls, runner = [], SignedOutcomeRunner("unknown", "broker_restarted", cleanup=False)
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    pending = advance_code_validation(proposal.id, client=runner)
    assert pending["status"] == "validating" and pending["error_code"] == "runner_cleanup_pending"
    runner.cleanup = True
    assert advance_code_validation(proposal.id, client=runner)["status"] == "unknown"
    assert len(runner.submissions) == 1 and len(calls) == 1


def test_forged_unknown_receipt_hard_fails_and_cannot_trigger_second_candidate(db):
    proposal, _ = seed(db)
    calls, runner = [], SignedOutcomeRunner("unknown", "broker_restarted", corrupt_signature=True)
    generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner)
    advance_code_validation(proposal.id, client=runner)
    advance_code_validation(proposal.id, client=runner)
    result = advance_code_validation(proposal.id, client=runner)
    assert result["status"] == "failed" and result["error_code"] == "runner_receipt_invalid"
    assert generate_code_candidate(proposal.id, gateway=gateway(candidate(), calls), client=runner) == "failed"
    assert len(calls) == 1 and len(runner.submissions) == 1
