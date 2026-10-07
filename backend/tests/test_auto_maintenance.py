from copy import deepcopy
import json
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from app.adapter_maintenance import activate_release, ensure_registry, new_candidate, validate_release
from app.auto_maintenance import (
    candidate_repairs_baseline, evidence_passes, execute_repair_incident, finish_recovery,
    prepare_recovery, publish_and_recover, queue_repair_incident,
)
from app.auto_maintenance_models import AdapterRepairIncident
from app.collection import CollectionError, JobDocument
from app.db import session_factory
from app.ingest import ingest
from app.llm import ModelGateway
from app.maintenance_models import AdapterRelease
from app.models import (
    Company, CompanyRun, Job, JobSourceRecord, ModelCall, SourceAccount, SourceBinding, SourceRun,
    Subject, TaskRun,
)
from app.platform_collectors import collect_platform
from app.repair_evidence import capture_detail, capture_repairs, capture_response
from app.serialization import row
from app.tasks import scope_digest
import test_platform_collectors as platform_test_support
import test_adapter_maintenance as maintenance_test_support

fake_browser = platform_test_support.fake_browser
manager = maintenance_test_support.manager


DESCRIPTION = "岗位职责：负责企业平台研发与系统架构设计，建设稳定的数据处理流程，参与接口开发与性能优化，和产品团队协作完成需求迭代。任职要求：熟悉软件工程和数据库。"


def seed(db, *, enabled=True, auto_publish=True):
    subject = Subject(name="甲公司")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name="甲公司")
    db.add(company)
    db.flush()
    account = SourceAccount(kind="boss", name="隔离测试账号", health="saved")
    db.add(account)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="boss", account_id=account.id,
                             entry_url="https://www.zhipin.com/web/geek/job")
    unrelated = SourceBinding(company_id=company.id, kind="careers", entry_url="https://careers.example/jobs")
    db.add_all([binding, unrelated])
    db.flush()
    snapshot = row(binding)
    identity = {"name": company.name, "aliases": []}
    task = TaskRun(company_id=company.id, status="failed", scope_hash="original-scope", config_snapshot={
        "company_name": company.name, "company_identity": identity,
        "max_pages": 1, "max_details": 1, "sources_snapshot": [snapshot, row(unrelated)],
    })
    db.add(task)
    db.flush()
    db.add(CompanyRun(task_run_id=task.id, company_id=company.id, scope_hash=task.scope_hash))
    source = SourceRun(task_id=task.id, binding_id=binding.id, status="failed", scope_hash="original-scope",
                       error_code="source_contract_changed")
    db.add(source)
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = registry.auto_repair_enabled = enabled
    registry.auto_publish_enabled = auto_publish
    db.commit()
    return company, account, binding, snapshot, identity, task, source, registry


def detail_evidence(snapshot, identity):
    doc = JobDocument(source_job_id="job1", title="工程师", url="https://www.zhipin.com/job_detail/job1.html",
                      recruiting_entity="甲公司")
    html = '<html><nav>登录账号：私人用户</nav><div class="jd-v2" data-token="secret">' + DESCRIPTION + (
        '<script>privateToken="do-not-send"</script><input value="cookie-value"></div></html>')
    with capture_repairs("boss", snapshot, identity) as captured:
        capture_detail(html, doc)
    return captured["evidence"]


def enqueue(db, state, evidence=None, code="source_contract_changed"):
    _, _, _, snapshot, identity, task, source, registry = state
    baseline = db.get(AdapterRelease, registry.active_release_id)
    incident = queue_repair_incident(db, task.id, source.id, snapshot, code,
                                     evidence if evidence is not None else detail_evidence(snapshot, identity), baseline.fingerprint)
    db.commit()
    return incident


def model(monkeypatch, manifest, callback=None):
    calls = []
    def response(request):
        data = json.loads(request.content)
        calls.append(data)
        if callback:
            callback()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(manifest)}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 200}})
    monkeypatch.setattr("app.auto_maintenance.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    return calls


def fix_manifest(db, registry, selector=".jd-v2"):
    payload = deepcopy(db.get(AdapterRelease, registry.active_release_id).manifest)
    payload["version"] = "automatic-fixed"
    payload["detail_selectors"].append(selector)
    return payload


def test_actual_detail_failure_agent_repair_publish_recapture_ingest_and_finish(db, fake_browser, monkeypatch):
    state = seed(db)
    company, account, binding, snapshot, identity, original, source, registry = state
    fake_browser.update(detail_html='<div class="jd-v2">' + DESCRIPTION + '</div>',
                        selector_descriptions={".jd-v2": DESCRIPTION}, detail_text=DESCRIPTION)
    with pytest.raises(CollectionError) as failure:
        collect_platform(account.id, "boss", snapshot, identity, {"max_pages": 1, "max_details": 1})
    error = failure.value
    assert error.code == "source_contract_changed"
    assert error.repair_evidence["type"] == "detail_dom"
    incident = queue_repair_incident(db, original.id, source.id, snapshot, error.code,
                                     error.repair_evidence, error.adapter_fingerprint)
    db.commit()
    requests = model(monkeypatch, fix_manifest(db, registry))
    execute_repair_incident(incident.id)
    db.refresh(incident)
    assert incident.status == "recovery_queued" and incident.attempts == 1 and incident.recovery_attempts == 1
    recovery = db.get(TaskRun, incident.recovery_task_id)
    assert len(recovery.config_snapshot["sources_snapshot"]) == 1
    recovery_snapshot = recovery.config_snapshot["sources_snapshot"][0]
    assert recovery_snapshot["id"] == str(binding.id)
    recovery.status, recovery.attempt = "running", 1
    db.commit()
    prepare_recovery(db, recovery.id)
    db.commit()
    result = collect_platform(account.id, "boss", recovery_snapshot, identity, {"max_pages": 1, "max_details": 1})
    run = SourceRun(task_id=recovery.id, binding_id=binding.id,
                    scope_hash=scope_digest({"source": "original-scope", "plan": result.adapter_fingerprint}),
                    adapter_version="boss:" + result.adapter_fingerprint[:12])
    db.add(run)
    db.flush()
    ingest(db, recovery, binding, run, result)
    recovery.status = "partial"
    finish_recovery(db, recovery.id)
    db.commit()
    db.refresh(incident)
    assert incident.status == "recovered"
    assert db.scalar(select(JobSourceRecord)).data["description"] == DESCRIPTION
    assert db.scalar(select(func.count()).select_from(Job)) == 1
    assert len(requests) == 1 and db.scalar(select(func.count()).select_from(ModelCall)) == 1
    assert "secret" not in requests[0]["messages"][-1]["content"]
    assert db.get(TaskRun, original.id).status == "failed"  # Original failure remains auditable.


@pytest.mark.parametrize("code", ["waiting_login", "waiting_verification", "dns_error", "http_error", "ReadTimeout", "identity_review"])
def test_non_parse_failures_never_enqueue_paid_repair(db, code):
    state = seed(db)
    assert enqueue(db, state, code=code) is None
    assert db.scalar(select(func.count()).select_from(AdapterRepairIncident)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_disabled_agent_and_missing_evidence_do_not_call_model(db):
    state = seed(db, enabled=False)
    assert enqueue(db, state) is None
    registry = state[-1]
    registry.agent_enabled = registry.auto_repair_enabled = True
    db.commit()
    incident = enqueue(db, state, evidence={})
    assert incident.status == "needs_engineering"
    execute_repair_incident(incident.id)
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_event_deduplication_and_two_candidate_budget(db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    assert enqueue(db, state).id == incident.id
    assert enqueue(db, state, code="selector_mismatch").id == incident.id
    baseline = db.get(AdapterRelease, state[-1].active_release_id).manifest
    requests = model(monkeypatch, baseline)  # Unchanged candidate cannot repair actual evidence.
    execute_repair_incident(incident.id)
    execute_repair_incident(incident.id)
    db.refresh(incident)
    assert incident.status == "needs_engineering" and incident.attempts == 2
    assert len(requests) == 2 and incident.recovery_task_id is None


def test_detail_evidence_is_job_only_and_redacts_attributes_contacts(db):
    state = seed(db)
    evidence = detail_evidence(state[3], state[4])
    encoded = json.dumps(evidence, ensure_ascii=False)
    assert "privateToken" not in encoded and "cookie-value" not in encoded and "data-token" not in encoded
    assert "私人用户" not in encoded and "jd-v2" in encoded


def test_capture_removes_comments_phone_email_and_bearer_data(db):
    state = seed(db)
    doc = JobDocument(source_job_id="job1", title="工程师", url="https://www.zhipin.com/job_detail/job1.html")
    html = '<div class="jd-v2">' + DESCRIPTION + (
        '<!--cookie=private-hidden--><p>电话13812345678，010-88888888；邮箱person@example.com；'
        'Bearer private-access-token</p></div>')
    with capture_repairs("boss", state[3], state[4]) as captured:
        capture_detail(html, doc)
    data = json.dumps(captured["evidence"], ensure_ascii=False)
    assert not any(value in data for value in ["private-hidden", "13812345678", "010-88888888", "person@example.com", "private-access-token"])


def test_moved_json_list_path_is_captured_without_navigation_secrets(db, monkeypatch):
    state = seed(db)
    baseline = deepcopy(db.get(AdapterRelease, state[-1].active_release_id).manifest)
    payload = {"zpData": {"vacanciesV2": [{"encryptJobId": "job1", "jobName": "工程师", "brandName": "甲公司",
                                              "securityId": "private-navigation", "bossName": "个人姓名", "phone": "13812345678"}]}}
    with capture_repairs("boss", state[3], state[4]) as capture:
        capture_response(payload, baseline)
    evidence = capture["evidence"]
    assert evidence["type"] == "response_list"
    assert not any(secret in json.dumps(evidence, ensure_ascii=False) for secret in ["private-navigation", "个人姓名", "13812345678"])
    candidate = deepcopy(baseline)
    candidate["version"] = "list-v2"
    candidate["list_paths"].append(["zpData", "vacanciesV2"])
    assert not evidence_passes(baseline, evidence)
    assert candidate_repairs_baseline(candidate, baseline, evidence)
    incident = enqueue(db, state, evidence)
    requests = model(monkeypatch, candidate)
    execute_repair_incident(incident.id)
    db.refresh(incident)
    assert incident.status == "recovery_queued" and len(requests) == 1


def test_unproven_selector_cannot_hide_alongside_a_real_fix(db):
    state = seed(db)
    baseline = db.get(AdapterRelease, state[-1].active_release_id).manifest
    candidate = fix_manifest(db, state[-1])
    candidate["detail_selectors"].insert(len(baseline["detail_selectors"]), ".logged-in-user")
    assert not candidate_repairs_baseline(candidate, baseline, detail_evidence(state[3], state[4]))


def test_publication_policy_waits_and_explicit_publish_queues_only_one_recovery(db, monkeypatch):
    state = seed(db, auto_publish=False)
    incident = enqueue(db, state)
    model(monkeypatch, fix_manifest(db, state[-1]))
    execute_repair_incident(incident.id)
    db.refresh(incident)
    assert incident.status == "awaiting_publish" and incident.recovery_task_id is None
    task = publish_and_recover(db, incident.id)
    db.commit()
    assert task.config_snapshot["repair_recovery"] is True
    with pytest.raises(CollectionError):
        publish_and_recover(db, incident.id)
    assert incident.recovery_attempts == 1


def test_cancel_during_model_request_stops_publication(db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    original_release = state[-1].active_release_id
    def cancel():
        with session_factory()() as other:
            other.get(AdapterRepairIncident, incident.id).cancel_requested = True
            other.commit()
    requests = model(monkeypatch, fix_manifest(db, state[-1]), cancel)
    execute_repair_incident(incident.id)
    db.refresh(incident)
    db.refresh(state[-1])
    assert incident.status == "cancelled" and state[-1].active_release_id == original_release
    assert len(requests) == 1 and incident.recovery_task_id is None


def test_version_conflict_during_model_request_stops_publish(db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    candidate = fix_manifest(db, state[-1])
    def publish_other():
        with session_factory()() as other:
            manifest = deepcopy(candidate)
            manifest["version"] = "another-admin-version"
            release = new_candidate(other, "boss", manifest)
            validate_release(other, release.id)
            activate_release(other, release.id)
            other.commit()
    model(monkeypatch, candidate, publish_other)
    execute_repair_incident(incident.id)
    db.refresh(incident)
    assert incident.status == "needs_engineering" and incident.recovery_task_id is None
    assert incident.details["last_error"] == "release_conflict"


def test_recovery_cas_and_attempt_limit_prevent_second_fetch(db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    model(monkeypatch, fix_manifest(db, state[-1]))
    execute_repair_incident(incident.id)
    db.refresh(incident)
    recovery = db.get(TaskRun, incident.recovery_task_id)
    recovery.attempt, recovery.status = 1, "running"
    db.commit()
    prepare_recovery(db, recovery.id)
    db.commit()
    with pytest.raises(CollectionError) as exc:
        prepare_recovery(db, recovery.id)
    assert exc.value.code == "recovery_budget_exhausted"
    recovery.attempt = 2
    db.commit()
    with pytest.raises(CollectionError):
        prepare_recovery(db, recovery.id)


def test_failed_recovery_cannot_requeue_repair_and_rolls_back_if_still_current(db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    original_release = state[-1].active_release_id
    model(monkeypatch, fix_manifest(db, state[-1]))
    execute_repair_incident(incident.id)
    db.refresh(incident)
    recovery = db.get(TaskRun, incident.recovery_task_id)
    failed_source = SourceRun(task_id=recovery.id, binding_id=incident.binding_id, status="failed", scope_hash="recovery",
                              error_code="source_contract_changed")
    db.add(failed_source)
    db.flush()
    assert queue_repair_incident(db, recovery.id, failed_source.id, recovery.config_snapshot["sources_snapshot"][0],
                                 "source_contract_changed", incident.evidence, "x" * 64) is None
    recovery.status = "failed"
    finish_recovery(db, recovery.id)
    db.commit()
    db.refresh(state[-1])
    assert incident.status == "failed" and state[-1].active_release_id == original_release


def test_real_company_worker_and_internal_response_drive_full_automatic_recovery(db, fake_browser, monkeypatch):
    from app.browser_internal import router
    from app.workers import _execute_company

    state = seed(db)
    _, _, _, snapshot, _, original, source, registry = state
    original.status, original.config_snapshot = "queued", {**original.config_snapshot, "sources_snapshot": [snapshot]}
    source.status = "queued"
    db.commit()
    fake_browser.update(detail_html='<div class="jd-v2">' + DESCRIPTION + '</div>',
                        selector_descriptions={".jd-v2": DESCRIPTION}, detail_text=DESCRIPTION)
    model_requests = model(monkeypatch, fix_manifest(db, registry))
    app = FastAPI()
    app.include_router(router)
    real_http_client = httpx.Client
    responses = []
    class Lease:
        def acquire(self):
            return True
        def release(self):
            pass
    class Redis:
        def lock(self, *args, **kwargs):
            return Lease()
    monkeypatch.setattr("app.browser_internal.redis.Redis.from_url", lambda *args, **kwargs: Redis())
    with TestClient(app) as internal:
        class InternalClient:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def post(self, url, **kwargs):
                response = internal.post(urlparse(url).path, **kwargs)
                responses.append(response.json())
                return response
        def client_factory(*args, **kwargs):
            # Only the internal worker HTTP hop is in-process; model calls keep MockTransport.
            return real_http_client(*args, **kwargs) if kwargs.get("transport") is not None else InternalClient()
        monkeypatch.setattr("app.workers.httpx.Client", client_factory)
        _execute_company(original.id)
        db.expire_all()
        incident = db.scalar(select(AdapterRepairIncident).where(AdapterRepairIncident.task_id == original.id))
        assert incident is not None and incident.status == "queued"
        assert responses[0]["error_code"] == "source_contract_changed"
        assert responses[0]["repair_evidence"]["type"] == "detail_dom"
        assert db.get(SourceRun, source.id).status == "failed"
        execute_repair_incident(incident.id)
        db.refresh(incident)
        recovery_id = incident.recovery_task_id
        assert recovery_id and incident.status == "recovery_queued"
        assert "adapter_versions" not in db.get(TaskRun, recovery_id).config_snapshot
        assert "resume_from_task_id" not in db.get(TaskRun, recovery_id).config_snapshot
        _execute_company(recovery_id)
        db.expire_all()
        assert db.get(AdapterRepairIncident, incident.id).status == "recovered"
        assert db.get(TaskRun, recovery_id).status == "partial"
        assert db.get(TaskRun, original.id).status == "failed"
        assert db.scalar(select(JobSourceRecord)).data["description"] == DESCRIPTION
        assert len(responses) == 2 and len(model_requests) == 1
        assert responses[1]["adapter_fingerprint"] != responses[0]["adapter_fingerprint"]


def test_auto_policy_requires_explicit_agent_and_can_be_disabled_without_model_calls(manager, db):
    seed(db, enabled=False)
    path = "/api/v1/adapter-maintenance/adapters/boss/settings"
    assert manager.patch(path, json={"auto_repair_enabled": True}).status_code == 422
    enabled = manager.patch(path, json={"agent_enabled": True, "auto_repair_enabled": True, "auto_publish_enabled": True})
    assert enabled.status_code == 200 and enabled.json()["auto_repair_enabled"] is True
    disabled = manager.patch(path, json={"agent_enabled": False})
    assert disabled.status_code == 200 and disabled.json()["auto_repair_enabled"] is False
    assert disabled.json()["auto_publish_enabled"] is False
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_incident_api_cancel_prevents_processing(manager, db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    response = manager.post(f"/api/v1/adapter-maintenance/incidents/{incident.id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    monkeypatch.setattr("app.auto_maintenance.ModelGateway", lambda: pytest.fail("cancelled event must not invoke model"))
    execute_repair_incident(incident.id)
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_generic_release_publish_keeps_incident_recovery_link(manager, db, monkeypatch):
    state = seed(db, auto_publish=False)
    incident = enqueue(db, state)
    model(monkeypatch, fix_manifest(db, state[-1]))
    execute_repair_incident(incident.id)
    db.refresh(incident)
    response = manager.post(f"/api/v1/adapter-maintenance/releases/{incident.release_id}/publish")
    assert response.status_code == 200 and response.json()["recovery_task_id"]
    db.refresh(incident)
    assert incident.status == "recovery_queued" and incident.recovery_attempts == 1


def test_already_parseable_evidence_does_not_justify_paid_repair(db):
    state = seed(db)
    evidence = detail_evidence(state[3], state[4])
    evidence["html"] = evidence["html"].replace("jd-v2", "job-sec-text")
    incident = enqueue(db, state, evidence)
    assert incident.status == "needs_engineering"
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_account_expiring_after_failure_blocks_model_request(db, monkeypatch):
    state = seed(db)
    incident = enqueue(db, state)
    state[1].health = "expired"
    db.commit()
    monkeypatch.setattr("app.auto_maintenance.ModelGateway", lambda: pytest.fail("expired account must not trigger paid repair"))
    execute_repair_incident(incident.id)
    db.refresh(incident)
    assert incident.status == "needs_engineering" and incident.attempts == 0
    assert incident.details["last_error"] == "waiting_login"


def test_recovery_expected_fingerprint_stops_before_browser(db, monkeypatch):
    state = seed(db)
    bad_snapshot = {**state[3], "expected_adapter_fingerprint": "0" * 64}
    monkeypatch.setattr("app.platform_collectors._collect_platform", lambda *args, **kwargs: pytest.fail("wrong release must not fetch"))
    with pytest.raises(CollectionError) as exc:
        collect_platform(state[1].id, "boss", bad_snapshot, state[4], {})
    assert exc.value.code == "adapter_version_conflict"
