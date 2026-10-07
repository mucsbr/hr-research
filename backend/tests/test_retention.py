from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import hashlib
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_factory
from app.models import BrowserLoginSession, ModelCall, RawDocument, Report, SourceAccount, TaskEvent, TaskRun, User, UserSession, now
from app.retention_models import RetentionPolicy, RetentionRun
from app.retention_service import (
    create_preview, create_run, ensure_policies, execute_run, policies_response, run_retention,
)


@pytest.fixture
def clock(db, monkeypatch):
    at = now()
    monkeypatch.setattr("app.retention_service.now", lambda: at)
    root = get_settings().storage_dir
    assert "pytest-" in str(root), "Deletion tests must use the db fixture's independent temporary directory"
    root.mkdir(parents=True, exist_ok=True)
    return at


def ready(db, clock):
    ensure_policies(db, clock)
    for policy in db.scalars(select(RetentionPolicy)):
        policy.retention_days = 1
        policy.grace_until = clock - timedelta(days=1)
    db.commit()


def task(db, clock, status="succeeded"):
    item = TaskRun(status=status, created_at=clock - timedelta(days=30), finished_at=clock - timedelta(days=20))
    db.add(item)
    db.flush()
    return item


def raw(db, clock, task_run=None, *, content="原始公开岗位", absolute=False, age=10):
    owner = task_run or task(db, clock)
    sha = hashlib.sha256(content.encode()).hexdigest()
    relative = "tasks/" + str(owner.id) + "/" + sha + ".txt"
    path = get_settings().storage_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    item = RawDocument(task_id=owner.id, url="https://example.com/jobs/1", sha256=sha,
                       storage_ref=str(path) if absolute else relative, created_at=clock - timedelta(days=age))
    db.add(item)
    db.commit()
    return item, path


def queued(db, category, clock, limit=100):
    preview = create_preview(db, categories=[category], limit=limit, at=clock)
    run, created = create_run(db, preview.id, str(uuid.uuid4()), at=clock)
    assert created
    return run


def test_default_policies_have_finite_visible_grace_and_do_not_delete(db, clock):
    document, path = raw(db, clock)
    data = policies_response(db, can_manage=True)
    assert {p["category"]: p["retention_days"] for p in data["policies"]} == {
        "raw_documents": 90, "reports": 180, "task_logs": 90, "expired_sessions": 7}
    assert all(p["enabled"] for p in data["policies"])
    preview = create_preview(db, at=clock)
    assert not preview.items and all(group["state"] == "grace_period" for group in preview.summary["categories"])
    assert run_retention()["status"] == "idle"
    assert path.exists() and db.get(RawDocument, document.id).cleared_at is None


@pytest.mark.parametrize("absolute", [False, True])
def test_raw_absolute_and_relative_tombstone_preserves_hash_and_task(db, clock, absolute):
    ready(db, clock)
    document, path = raw(db, clock, absolute=absolute)
    original = (document.sha256, document.url, document.storage_ref)
    run = queued(db, "raw_documents", clock)
    result = execute_run(run.id, at=clock)
    assert result["status"] == "succeeded" and result["summary"]["files_deleted"] == 1
    assert not path.exists()
    db.expire_all()
    document = db.get(RawDocument, document.id)
    assert document.cleared_at == clock and (document.sha256, document.url, document.storage_ref) == original
    assert db.get(TaskRun, document.task_id)
    assert execute_run(run.id, at=clock)["summary"] == result["summary"]
    assert not create_preview(db, categories=["raw_documents"], at=clock).items


@pytest.mark.parametrize("change", ["task_restart", "raw_reference", "task_reassignment", "policy_disabled"])
def test_final_gate_reloads_second_session_change_after_durable_intent(db, clock, change):
    ready(db, clock)
    owner = task(db, clock)
    document, path = raw(db, clock, owner)
    log = TaskEvent(task_id=owner.id, message="仍需保留的运行记录", created_at=clock - timedelta(days=10))
    db.add(log)
    db.commit()
    category = "task_logs" if change == "task_restart" else "raw_documents"
    run = queued(db, category, clock)
    changed = []

    def after_commit(session):
        if changed or not any(isinstance(item, RetentionRun) and any(entry["status"] == "deleting" for entry in item.items)
                              for item in session.identity_map.values()):
            return
        changed.append(True)
        with session_factory()() as other:
            if change == "task_restart":
                other_task = other.get(TaskRun, owner.id)
                other_task.status, other_task.attempt = "running", other_task.attempt + 1
            elif change == "raw_reference":
                other_raw = other.get(RawDocument, document.id)
                other_raw.storage_ref = "tasks/" + str(owner.id) + "/" + "a" * 64 + ".txt"
                other_raw.sha256 = "a" * 64
            elif change == "task_reassignment":
                new_task = TaskRun(status="running")
                other.add(new_task)
                other.flush()
                other.get(RawDocument, document.id).task_id = new_task.id
            else:
                policy = other.scalar(select(RetentionPolicy).where(RetentionPolicy.category == category))
                policy.enabled, policy.revision = False, policy.revision + 1
            other.commit()

    event.listen(Session, "after_commit", after_commit)
    try:
        result = execute_run(run.id, at=clock)
    finally:
        event.remove(Session, "after_commit", after_commit)
    assert changed
    assert result["items"][0]["status"] == "skipped"
    assert path.exists()
    db.expire_all()
    assert db.get(TaskEvent, log.id) is not None
    assert db.get(RawDocument, document.id).cleared_at is None


def test_shared_relative_absolute_file_is_deleted_only_after_last_live_reference(db, clock):
    ready(db, clock)
    first, path = raw(db, clock)
    second = RawDocument(task_id=first.task_id, url="https://example.com/other", sha256=first.sha256,
                         storage_ref=str(path), created_at=clock)
    db.add(second)
    db.commit()
    result = execute_run(queued(db, "raw_documents", clock).id, at=clock)
    assert result["summary"]["cleared"] == 1 and result["summary"]["files_deleted"] == 0
    assert path.exists()
    second.created_at = clock - timedelta(days=10)
    db.commit()
    result = execute_run(queued(db, "raw_documents", clock).id, at=clock)
    assert result["summary"]["files_deleted"] == 1 and not path.exists()
    assert db.scalar(select(func.count()).select_from(RawDocument)) == 2


@pytest.mark.parametrize("unsafe", ["outside", "traversal", "symlink_file", "symlink_parent", "code_directory", "hash_changed"])
def test_unsafe_or_changed_paths_never_delete_unapproved_bytes(db, clock, tmp_path, unsafe):
    ready(db, clock)
    document, path = raw(db, clock)
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    if unsafe == "outside":
        document.storage_ref = str(outside)
    elif unsafe == "traversal":
        document.storage_ref = "../outside.txt"
    elif unsafe == "code_directory":
        document.storage_ref = "adapter-code/" + document.sha256 + ".json"
        protected = get_settings().storage_dir / document.storage_ref
        protected.parent.mkdir()
        protected.write_text("code artifact")
    elif unsafe == "symlink_file":
        path.unlink()
        path.symlink_to(outside)
    elif unsafe == "symlink_parent":
        path.unlink()
        path.parent.rmdir()
        directory = tmp_path / "outside-dir"
        directory.mkdir()
        (directory / path.name).write_text("must survive")
        path.parent.symlink_to(directory, target_is_directory=True)
    else:
        path.write_text("changed independently")
    db.commit()
    result = execute_run(queued(db, "raw_documents", clock).id, at=clock)
    assert result["status"] == "partial" and result["summary"]["files_deleted"] == 0
    assert outside.read_text() == "must survive"
    if unsafe not in {"outside", "traversal", "code_directory"}:
        assert path.exists()
    if unsafe == "code_directory":
        assert protected.read_text() == "code artifact"


def test_adapter_package_reference_protects_shared_file(db, clock):
    from app.adapter_code_models import AdapterCodePackage
    ready(db, clock)
    document, path = raw(db, clock)
    package = AdapterCodePackage(bundle_sha="a" * 64, kind="boss", file_ref=document.storage_ref,
                                 lock_sha="b" * 64, files_manifest={}, dependencies=[], provenance={})
    db.add(package)
    db.commit()
    result = execute_run(queued(db, "raw_documents", clock).id, at=clock)
    assert result["summary"]["files_deleted"] == 0 and path.exists()
    assert db.get(AdapterCodePackage, package.id).file_ref == document.storage_ref


def make_report(db, clock):
    owner = task(db, clock)
    owner.domain = "sentiment"
    prefix = get_settings().storage_dir / "bettafish" / str(owner.id)
    contents = {
        "request.json": '{"evidence":"original body"}', "worker.log": "original model material",
        "result.html": "<p>readable report</p>", "upstream-result.html": "<p>original report</p>",
        "logs/report.log": "report log", "reports/final_report_sample.html": "full HTML",
        "reports/report_state_sample.json": '{"body":"full report state"}', "ir/report_ir_sample.json": "full IR",
        "json-errors/error.json": "invalid model response",
        "chapters/report-deadbeef/manifest.json": "report metadata",
        "chapters/report-deadbeef/001-intro/chapter.json": "complete chapter",
        "chapters/report-deadbeef/001-intro/stream.raw": "complete stream",
    }
    for relative, content in contents.items():
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    report = Report(task_id=owner.id, domain="sentiment", title="原报告标题", content="完整分析正文",
        evidence=[{"url": "https://example.com/original", "title": "原文", "description": "完整原文"}],
        coverage={"engine": "bettafish", "subject_id": "fixture", "analysis": {"summary": "完整分析副本"},
                  "artifact_sha256": hashlib.sha256(contents["result.html"].encode()).hexdigest(),
                  "upstream_artifact_sha256": hashlib.sha256(contents["upstream-result.html"].encode()).hexdigest()},
        created_at=clock - timedelta(days=10))
    db.add(report)
    db.commit()
    return report, prefix, contents


def test_nested_upstream_report_copies_cleared_but_metadata_and_unknown_paths_survive(db, clock, signed_client):
    ready(db, clock)
    report, prefix, contents = make_report(db, clock)
    protected = ["cache/keep.json", "matplotlib-cache/keep.json", "chapters/not-an-upstream-run/body.json",
                 "chapters/report-deadbeef/unknown/body.json", "reports/unregistered.txt"]
    for relative in protected:
        path = prefix / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("protected")
    call = ModelCall(task_id=report.task_id, purpose="fixture", model="test", status="completed", prompt_tokens=5, completion_tokens=3)
    db.add(call)
    db.commit()
    result = execute_run(queued(db, "reports", clock).id, at=clock)
    assert result["status"] == "succeeded" and result["summary"]["files_deleted"] == len(contents)
    assert all(not (prefix / path).exists() for path in contents)
    assert all((prefix / path).read_text() == "protected" for path in protected)
    db.expire_all()
    report = db.get(Report, report.id)
    assert report.content == "" and report.cleared_at == clock
    assert "analysis" not in report.coverage and "description" not in report.evidence[0]
    assert report.retention["content_sha256"] == hashlib.sha256("完整分析正文".encode()).hexdigest()
    assert report.evidence[0]["url"] == "https://example.com/original"
    assert db.get(ModelCall, call.id).prompt_tokens == 5
    for route in [f"/api/v1/reports/{report.id}", f"/api/v1/sentiment/reports/{report.id}"]:
        response = signed_client.get(route)
        assert response.status_code == 200 and response.json()["content"] == "" and response.json()["cleared_at"]
        assert response.json()["evidence"][0]["url"] == "https://example.com/original"
    assert signed_client.get(f"/api/v1/sentiment/reports/{report.id}/artifact").status_code == 410
    report.domain = "recruitment"
    db.commit()
    assert signed_client.get(f"/api/v1/sentiment/reports/{report.id}/artifact").status_code == 404


def test_partial_file_failure_is_audited_and_can_be_reconciled(db, clock, monkeypatch):
    import app.retention_service as service
    ready(db, clock)
    report, prefix, _ = make_report(db, clock)
    original_unlink = service.os.unlink
    def deny_request(path, *args, **kwargs):
        if path == "request.json":
            raise PermissionError("fixture only")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(service.os, "unlink", deny_request)
    result = execute_run(queued(db, "reports", clock).id, at=clock)
    assert result["status"] == "partial" and result["summary"]["failed"] == 1
    assert (prefix / "request.json").exists() and not (prefix / "result.html").exists()
    db.expire_all()
    assert db.get(Report, report.id).content == "" and db.get(Report, report.id).retention["files_pending"]
    monkeypatch.setattr(service.os, "unlink", original_unlink)
    later = clock + timedelta(days=2)
    run = queued(db, "reports", later)
    assert execute_run(run.id, at=later)["status"] == "succeeded"
    assert not (prefix / "request.json").exists()


def test_commit_failure_after_unlink_keeps_intent_and_partial_history(db, clock, monkeypatch):
    ready(db, clock)
    document, path = raw(db, clock)
    run = queued(db, "raw_documents", clock)
    real_commit, failures = Session.commit, []
    def fail_once(session):
        if not failures and not path.exists() and any(isinstance(obj, RetentionRun) for obj in session.identity_map.values()):
            failures.append(True)
            raise RuntimeError("simulated commit failure after unlink")
        return real_commit(session)
    monkeypatch.setattr(Session, "commit", fail_once)
    result = execute_run(run.id, at=clock)
    assert failures and result["status"] == "partial"
    assert result["items"][0]["reason"] == "cleanup_interrupted" and not path.exists()
    db.expire_all()
    assert db.get(RawDocument, document.id).cleared_at is None
    assert db.get(RetentionRun, run.id).items[0]["file_intents"]
    retry = execute_run(queued(db, "raw_documents", clock).id, at=clock)
    assert retry["status"] == "succeeded" and retry["summary"]["files_deleted"] == 0


def test_expired_sessions_only_preserves_live_session_and_account_profile(db, clock, tmp_path):
    ready(db, clock)
    user = User(username="session-owner", password_hash="fixture")
    profile = tmp_path / "account-profile.state.enc"
    profile.write_text("encrypted fixture")
    account = SourceAccount(kind="boss", name="profile owner", session_ref=str(profile), health="ready")
    db.add_all([user, account])
    db.flush()
    old = UserSession(user_id=user.id, token_hash="a" * 64, expires_at=clock - timedelta(days=10))
    live = UserSession(user_id=user.id, token_hash="b" * 64, expires_at=clock + timedelta(days=1))
    browser = BrowserLoginSession(account_id=account.id, owner_user_id=user.id, status="expired", expires_at=clock - timedelta(days=10))
    db.add_all([old, live, browser])
    db.commit()
    old_id, browser_id, live_id = old.id, browser.id, live.id
    result = execute_run(queued(db, "expired_sessions", clock).id, at=clock)
    assert result["summary"]["cleared"] == 2
    db.expire_all()
    assert db.get(UserSession, old_id) is None and db.get(BrowserLoginSession, browser_id) is None
    assert db.get(UserSession, live_id) and profile.read_text() == "encrypted fixture"
    assert db.get(SourceAccount, account.id).session_ref == str(profile)


def test_preview_expiry_changed_policy_and_idempotency_do_not_delete(db, clock):
    ready(db, clock)
    _, path = raw(db, clock)
    preview = create_preview(db, categories=["raw_documents"], at=clock)
    with pytest.raises(HTTPException) as expired:
        create_run(db, preview.id, "expired", at=clock + timedelta(seconds=901))
    assert expired.value.status_code == 409
    db.rollback()
    key = str(uuid.uuid4())
    first, made = create_run(db, preview.id, key, at=clock)
    second, repeated = create_run(db, preview.id, key, at=clock)
    assert made and not repeated and first.id == second.id and path.exists()
    with pytest.raises(HTTPException):
        create_run(db, preview.id, "different-key", at=clock)
    db.rollback()
    other = create_preview(db, categories=["raw_documents"], at=clock)
    policy = db.scalar(select(RetentionPolicy).where(RetentionPolicy.category == "raw_documents"))
    policy.revision += 1
    db.commit()
    with pytest.raises(HTTPException):
        create_run(db, other.id, str(uuid.uuid4()), at=clock)
    db.rollback()
    assert path.exists()


def test_concurrent_same_run_and_bounded_new_batches_are_idempotent(db, clock):
    ready(db, clock)
    documents = [raw(db, clock, content="不同岗位" + str(index)) for index in range(3)]
    preview = create_preview(db, categories=["raw_documents"], limit=1, at=clock)
    assert len(preview.items) == 1 and preview.summary["categories"][0]["has_more"]
    run, _ = create_run(db, preview.id, str(uuid.uuid4()), at=clock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute_run(run.id, at=clock), range(2)))
    assert any(result["status"] == "succeeded" for result in results)
    assert sum(path.exists() for _, path in documents) == 2
    assert execute_run(run.id, at=clock)["summary"]["files_deleted"] == 1
    assert execute_run(queued(db, "raw_documents", clock).id, at=clock)["summary"]["cleared"] == 2


def test_admin_policy_api_preview_run_lookup_and_read_only_user(signed_client, db, clock, monkeypatch):
    from app.security import hasher
    ready(db, clock)
    _, path = raw(db, clock)
    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture broker unavailable")
    monkeypatch.setattr("app.queue.celery.send_task", unavailable)
    policies = signed_client.get("/api/v1/retention/policies").json()
    assert policies["can_manage"] and policies["limits"]["batch_size_max"] == 200
    original = next(item for item in policies["policies"] if item["category"] == "raw_documents")
    bad = signed_client.put("/api/v1/retention/policies", json={"policies": [{"category": "raw_documents", "retention_days": 1.5, "enabled": True, "expected_revision": original["revision"]}]})
    assert bad.status_code == 422
    preview = signed_client.post("/api/v1/retention/preview", json={"categories": ["raw_documents"], "limit": 1}).json()
    assert preview["eligible_count"] == 1 and preview["estimated_bytes"] == path.stat().st_size
    key = str(uuid.uuid4())
    response = signed_client.post("/api/v1/retention/runs", json={"preview_id": preview["id"], "idempotency_key": key})
    assert response.status_code == 202 and response.json()["status"] == "queued" and path.exists()
    assert signed_client.get("/api/v1/retention/runs/by-key", params={"idempotency_key": key}).json()["id"] == response.json()["id"]
    assert signed_client.post("/api/v1/retention/runs", json={"preview_id": preview["id"], "idempotency_key": key}).json()["id"] == response.json()["id"]
    ordinary = User(username="retention-reader", password_hash=hasher.hash("reader-password-123"), role="user")
    db.add(ordinary)
    db.commit()
    assert signed_client.post("/api/v1/auth/login", json={"username": ordinary.username, "password": "reader-password-123"}).status_code == 200
    assert not signed_client.get("/api/v1/retention/policies").json()["can_manage"]
    assert signed_client.get("/api/v1/retention/runs").status_code == 200
    assert signed_client.post("/api/v1/retention/preview", json={}).status_code == 403
    assert signed_client.put("/api/v1/retention/policies", json={"policies": [{"category": "raw_documents", "retention_days": 2, "enabled": True, "expected_revision": original["revision"]}]}).status_code == 403


def test_shortening_reenable_and_stale_policy_revision_have_visible_grace(signed_client, db, clock, monkeypatch):
    ready(db, clock)
    _, path = raw(db, clock)
    def update(days, enabled, revision):
        return signed_client.put("/api/v1/retention/policies", json={"policies": [
            {"category": "raw_documents", "retention_days": days, "enabled": enabled, "expected_revision": revision}]})
    response = update(2, True, 1)
    assert response.status_code == 200 and response.json()["can_manage"]
    response = update(1, True, 2)
    policy = next(item for item in response.json()["policies"] if item["category"] == "raw_documents")
    assert policy["grace_until"] == (clock + timedelta(days=7)).isoformat()
    preview = signed_client.post("/api/v1/retention/preview", json={"categories": ["raw_documents"]}).json()
    assert not preview["can_execute"] and preview["categories"][0]["state"] == "grace_period"
    assert update(1, False, 3).status_code == 200
    assert signed_client.post("/api/v1/retention/preview", json={"categories": ["raw_documents"]}).json()["categories"][0]["state"] == "disabled"
    monkeypatch.setattr("app.retention_service.now", lambda: clock + timedelta(days=2))
    response = update(1, True, 4)
    policy = next(item for item in response.json()["policies"] if item["category"] == "raw_documents")
    assert policy["grace_until"] == (clock + timedelta(days=9)).isoformat()
    assert update(365, True, 1).status_code == 409
    assert path.exists()


def test_global_file_budget_is_bounded_and_later_run_finishes_registered_copies(db, clock, monkeypatch):
    import app.retention_service as service
    ready(db, clock)
    first, first_dir, first_files = make_report(db, clock)
    second, second_dir, second_files = make_report(db, clock)
    monkeypatch.setattr(service, "MAX_FILES", 4)
    result = execute_run(queued(db, "reports", clock).id, at=clock)
    assert result["status"] == "partial" and result["summary"]["files_deleted"] == 4
    remaining = sum((root / name).exists() for root, files in [(first_dir, first_files), (second_dir, second_files)] for name in files)
    assert remaining == len(first_files) + len(second_files) - 4
    monkeypatch.setattr(service, "MAX_FILES", 100)
    later = clock + timedelta(days=2)
    final = execute_run(queued(db, "reports", later).id, at=later)
    assert final["status"] == "succeeded" and final["summary"]["files_deleted"] == remaining
    assert first.id != second.id


def test_time_budget_stops_before_any_unlink_and_new_preview_can_continue(db, clock, monkeypatch):
    import app.retention_service as service
    ready(db, clock)
    _, path = raw(db, clock)
    run = queued(db, "raw_documents", clock)
    monkeypatch.setattr(service, "RUN_SECONDS", 0)
    result = execute_run(run.id, at=clock)
    assert result["status"] == "partial" and result["summary"]["files_deleted"] == 0 and path.exists()
    monkeypatch.setattr(service, "RUN_SECONDS", 30)
    assert execute_run(queued(db, "raw_documents", clock).id, at=clock)["status"] == "succeeded"


def test_raw_cleanup_keeps_job_history_fk_and_metrics(db, clock):
    from app.analytics_service import company_metrics
    from app.models import Company, Job, JobObservation, JobSourceRecord, JobVersion, SourceBinding, SourceRun, Subject
    ready(db, clock)
    document, path = raw(db, clock)
    subject = Subject(name="保留岗位历史公司")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name=subject.name)
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="careers", entry_url="https://example.com/jobs")
    job = Job(company_id=company.id, title="工程师")
    db.add_all([binding, job])
    db.flush()
    record = JobSourceRecord(job_id=job.id, binding_id=binding.id, source_job_id="job-1",
                             url=document.url, current_hash="a" * 64, baseline_scope="stable", data={"description": "结构化岗位JD"})
    source_run = SourceRun(task_id=document.task_id, binding_id=binding.id, status="succeeded", coverage="complete",
                           scope_hash="stable", counts={"observed": 1})
    db.add_all([record, source_run])
    db.flush()
    version = JobVersion(record_id=record.id, content_hash="a" * 64,
                         data={"title": job.title, "description": "结构化岗位JD", "url": document.url}, raw_document_id=document.id)
    db.add(version)
    db.flush()
    observation = JobObservation(record_id=record.id, source_run_id=source_run.id, version_id=version.id)
    db.add(observation)
    db.commit()
    before = company_metrics(db, company.id)
    assert before["observed_jobs"] == 1
    assert execute_run(queued(db, "raw_documents", clock).id, at=clock)["status"] == "succeeded"
    db.expire_all()
    assert not path.exists() and db.get(JobVersion, version.id).raw_document_id == document.id
    assert db.get(JobObservation, observation.id).version_id == version.id
    assert db.get(JobSourceRecord, record.id).data["description"] == "结构化岗位JD"
    assert company_metrics(db, company.id)["observed_jobs"] == before["observed_jobs"]
