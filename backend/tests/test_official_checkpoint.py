from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import subprocess
import sys
import time
from threading import Event, Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
import uuid

import pytest
from sqlalchemy import func, select

from app import collection
from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
from app.collection import CollectionError, JobDocument, collect_generic, collect_tencent
from app.config import get_settings
from app.db import session_factory
from app.models import Company, CompanyRun, JobChange, JobObservation, JobSourceRecord, SourceBinding, SourceRun, Subject, TaskRun
from app.official_checkpoint import OfficialCheckpointStore
from app.llm import ModelError
from app.serialization import row
from app.website_agent import collect_official


PLAN = {"render_mode": "http", "job_link_selector": ".job", "title_selector": "h1",
        "description_selector": ".description", "requirements_selector": None, "location_selector": None,
        "salary_selector": None, "next_selector": ".next", "complete_selector": ".end", "total_selector": ".total"}
FILTERS = {"cities": [], "keywords": [], "exclusions": [], "sources": ["careers"], "max_pages": 2, "max_details": 5}


@pytest.fixture
def clock(monkeypatch):
    value = {"now": datetime(2026, 10, 5, 15, 59, 50, tzinfo=timezone.utc)}
    monkeypatch.setattr("app.official_checkpoint.now", lambda: value["now"])
    return value


@pytest.fixture
def site(monkeypatch):
    state = {"requests": Counter(), "failures": {}, "callback": None, "total": 3, "changed_details": set()}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            state["requests"][self.path] += 1
            if state["callback"]:
                state["callback"](self.path)
            if state["failures"].get(self.path, 0):
                state["failures"][self.path] -= 1
                self.send_response(503)
                self.end_headers()
                return
            query = parse_qs(parsed.query)
            page = int(query.get("page", ["1"])[0])
            if parsed.path == "/list":
                keys = [1, 2] if page == 1 else [3]
                body = f'<div class="total">共{state["total"]}个职位</div>' + "".join(
                    f'<a class="job" href="/job/{key}">工程师{key}</a>' for key in keys)
                body += '<a class="next" href="/list?page=2">下一页</a>' if page == 1 else '<p class="end">没有更多职位</p>'
            elif parsed.path.startswith("/job/"):
                description_class = "changed-description" if parsed.path in state["changed_details"] else "description"
                body = f'<h1>工程师{parsed.path.rsplit("/", 1)[1]}</h1><div class="{description_class}">' + "负责公开岗位研发与维护，欢迎应聘。" * 15 + "</div>"
            elif parsed.path == "/structured":
                keys = [1, 2] if page == 1 else [3]
                jobs = [{"@type": "JobPosting", "title": f"岗位{key}", "identifier": str(key),
                         "description": "招聘职责", "url": f"/job/{key}"} for key in keys]
                body = '<script type="application/ld+json">' + json.dumps(jobs) + "</script>"
                body += '<a class="next" href="/structured?page=2">下一页</a>' if page == 1 else '<p class="end">没有更多职位</p>'
            elif parsed.path.endswith("/Query"):
                index = int(query["pageIndex"][0])
                body = json.dumps({"Code": 200, "Data": {"Count": 3, "Posts": [{"PostId": key} for key in ([1, 2] if index == 1 else [3])]}})
            elif parsed.path.endswith("/ByPostId"):
                key = query["postId"][0]
                body = json.dumps({"Code": 200, "Data": {"RecruitPostName": "岗位" + key,
                    "Responsibility": "研发职责", "Requirement": "经验要求", "LocationName": "上海"}})
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(get_settings(), "allow_local_fixture_fetch", True)
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def seed(db, url, clock, config=None, filters=None, adapter="generic", open_store=True):
    filters = dict(filters or FILTERS)
    subject = Subject(name="检查点验证样本")
    db.add(subject)
    db.flush()
    company = Company(name=subject.name, subject_id=subject.id)
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="careers", adapter=adapter, entry_url=url,
                            config={"website_plan": PLAN} if config is None else config)
    db.add(binding)
    task = TaskRun(company_id=company.id, status="running", attempt=1, started_at=clock["now"],
                   config_snapshot=filters, scope_hash="checkpoint-test")
    db.add(task)
    db.flush()
    db.add(CompanyRun(task_run_id=task.id, company_id=company.id, scope_hash=task.scope_hash))
    run = SourceRun(task_id=task.id, binding_id=binding.id, status="running", scope_hash="checkpoint-test")
    db.add(run)
    db.commit()
    snapshot = row(binding)
    store = OfficialCheckpointStore.open(task.id, snapshot, filters, run.id, task.attempt) if open_store else None
    return binding, task, run, snapshot, filters, store


def resume(db, task, run, snapshot, filters, clock):
    db.refresh(task)
    task.attempt += 1
    task.status, task.cancel_requested, task.started_at = "running", False, clock["now"]
    db.commit()
    return OfficialCheckpointStore.open(task.id, snapshot, filters, run.id, task.attempt)


def test_css_detail_failure_resumes_without_repeating_verified_requests(db, site, clock):
    base, state = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock)
    state["failures"]["/job/2"] = 1
    with pytest.raises(CollectionError, match="HTTP 503"):
        collect_official(snapshot, filters, task.id, checkpoint=store)
    steps = db.scalars(select(OfficialCheckpointStep)).all()
    assert len(steps) == 2 and sum(len(step.parsed_records) for step in steps) == 1
    clock["now"] += timedelta(seconds=20)  # Across midnight in Asia/Shanghai.
    restored = resume(db, task, run, snapshot, filters, clock)
    result = collect_official(snapshot, filters, task.id, checkpoint=restored)
    assert result.coverage == "complete" and len(result.records) == 3
    assert state["requests"]["/list"] == 1 and state["requests"]["/job/1"] == 1
    assert state["requests"]["/job/2"] == 2 and state["requests"]["/list?page=2"] == 1
    first = next(record for record in result.records if record.title == "工程师1")
    assert first.observed_at == clock["now"] - timedelta(seconds=20)
    assert result.checkpoint_info["resumed"] and result.checkpoint_info["collection_window_start"].startswith("2026-10-05T15:59:50")
    assert result.checkpoint_info["collection_window_end"].startswith("2026-10-05T16:00:10")


def test_detail_budget_and_page_budget_resume_frontier_instead_of_starting_over(db, site, clock):
    base, state = site
    filters = {**FILTERS, "max_pages": 1, "max_details": 1}
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters=filters)
    first = collect_official(snapshot, filters, task.id, checkpoint=store)
    assert first.coverage == "partial" and len(first.records) == 1
    second = collect_official(snapshot, filters, task.id, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert second.coverage == "partial" and len(second.records) == 2
    third = collect_official(snapshot, filters, task.id, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert third.coverage == "complete" and len(third.records) == 3
    assert all(value == 1 for value in state["requests"].values())


def test_jsonld_with_multiple_jobs_on_one_page_resumes_inside_page(db, site, clock):
    base, state = site
    _, task, run, snapshot, filters, store = seed(db, base + "/structured", clock,
        config={"next_selector": ".next", "complete_selector": ".end"}, filters={**FILTERS, "max_details": 1})
    first = collect_generic(snapshot, filters, checkpoint=store)
    assert len(first.records) == 1 and first.coverage == "partial"
    second = collect_generic(snapshot, filters, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert len(second.records) == 2 and second.coverage == "partial"
    final = collect_generic(snapshot, filters, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert len(final.records) == 3 and final.coverage == "complete"
    assert state["requests"] == Counter({"/structured": 1, "/structured?page=2": 1})


def test_tencent_cursor_resumes_page_and_pending_post_ids_using_local_http(db, site, clock, monkeypatch):
    base, state = site
    original = collection.PublicFetcher
    class LocalTencent:
        def __init__(self):
            self.inner = original()
        def fetch(self, url):
            parsed = urlparse(url)
            _, body = self.inner.fetch(base + parsed.path + "?" + parsed.query)
            return url, body
        def close(self):
            self.inner.close()
    monkeypatch.setattr(collection, "PublicFetcher", LocalTencent)
    _, task, run, snapshot, filters, store = seed(db, "https://careers.tencent.com/search.html", clock,
        config={}, filters={**FILTERS, "max_pages": 1, "max_details": 1}, adapter="tencent")
    values = []
    for attempt in range(3):
        if attempt:
            store = resume(db, task, run, snapshot, filters, clock)
        values.append(collect_tencent(snapshot, filters, checkpoint=store))
    assert [len(value.records) for value in values] == [1, 2, 3]
    assert [value.coverage for value in values] == ["partial", "partial", "complete"]
    assert len(state["requests"]) == 5 and all(value == 1 for value in state["requests"].values())


def test_validated_adaptation_is_saved_before_interruption_and_never_recalled(db, site, clock, monkeypatch):
    base, state = site
    binding, task, run, snapshot, filters, store = seed(db, base + "/list", clock, config={})
    calls = []
    class Planner:
        def complete(self, messages, **kwargs):
            calls.append(messages)
            assert len(messages[-1]["content"]) < 15000
            return SimpleNamespace(content=json.dumps(PLAN), call_id=uuid.uuid4(), model="fixture-model")
    monkeypatch.setattr("app.website_agent.ModelGateway", Planner)
    state["failures"]["/job/2"] = 1
    with pytest.raises(CollectionError, match="HTTP 503"):
        collect_official(snapshot, filters, task.id, checkpoint=store)
    assert len(calls) == 1
    db.refresh(binding)
    assert binding.config["website_plan"] == PLAN
    result = collect_official(snapshot, filters, task.id, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert result.coverage == "complete" and len(calls) == 1
    assert state["requests"]["/list"] == state["requests"]["/job/1"] == 1


def test_unknown_model_attempt_is_not_repeated_on_checkpoint_resume(db, site, clock, monkeypatch):
    base, state = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, config={})
    calls = []
    class UncertainPlanner:
        def complete(self, *args, **kwargs):
            calls.append(1)
            raise ModelError("timeout", "结果未知")
    monkeypatch.setattr("app.website_agent.ModelGateway", UncertainPlanner)
    with pytest.raises(CollectionError) as first:
        collect_official(snapshot, filters, task.id, checkpoint=store)
    assert first.value.code == "timeout"
    requests = dict(state["requests"])
    with pytest.raises(CollectionError) as second:
        collect_official(snapshot, filters, task.id, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert second.value.code == "checkpoint_adaptation_unconfirmed"
    assert calls == [1] and dict(state["requests"]) == requests


def test_changed_layout_after_partial_observation_does_not_mix_rule_versions(db, site, clock, monkeypatch):
    base, state = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    assert collect_official(snapshot, filters, task.id, checkpoint=store).coverage == "partial"
    state["changed_details"].add("/job/2")
    def forbidden():
        pytest.fail("A resumed snapshot with observed jobs must not mix in a newly learned rule")
    monkeypatch.setattr("app.website_agent.ModelGateway", forbidden)
    with pytest.raises(CollectionError) as caught:
        collect_official(snapshot, filters, task.id, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert caught.value.code == "checkpoint_rule_changed"
    assert sum(len(values) for values in db.scalars(select(OfficialCheckpointStep.parsed_records))) == 1
    assert state["requests"]["/job/1"] == 1


def test_explicit_retry_copies_only_same_scope_and_keeps_observation_times(db, site, clock):
    base, state = site
    binding, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    first = collect_official(snapshot, filters, task.id, checkpoint=store)
    old_observed = first.records[0].observed_at
    task.status, run.status = "partial", "partial"
    db.commit()
    clock["now"] += timedelta(minutes=5)
    new_task = TaskRun(company_id=task.company_id, status="running", attempt=1, started_at=clock["now"],
                       config_snapshot=filters, scope_hash=task.scope_hash)
    db.add(new_task)
    db.flush()
    new_run = SourceRun(task_id=new_task.id, binding_id=binding.id, status="running", scope_hash=run.scope_hash)
    db.add(new_run)
    db.commit()
    copied = OfficialCheckpointStore.open(new_task.id, snapshot, filters, new_run.id, 1, resume_from_task_id=task.id)
    result = collect_official(snapshot, filters, new_task.id, checkpoint=copied)
    assert result.checkpoint_info["resume_from_task_id"] == str(task.id)
    assert next(item for item in result.records if item.title == "工程师1").observed_at == old_observed
    assert state["requests"]["/list"] == state["requests"]["/job/1"] == 1
    continued = resume(db, new_task, new_run, snapshot, filters, clock)
    final = collect_official(snapshot, filters, new_task.id, checkpoint=continued)
    assert final.coverage == "complete" and final.checkpoint_info["resume_from_task_id"] == str(task.id)


@pytest.mark.parametrize("changed", ["url", "filter", "rule"])
def test_scope_changes_reject_old_checkpoint_without_any_http(db, site, clock, changed):
    base, state = site
    binding, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    collect_official(snapshot, filters, task.id, checkpoint=store)
    requests = dict(state["requests"])
    if changed == "url":
        binding.entry_url = base + "/other"
    elif changed == "rule":
        binding.config = {"website_plan": {**PLAN, "title_selector": "h2"}}
    else:
        filters = {**filters, "cities": ["上海"]}
    db.commit()
    with pytest.raises(CollectionError) as caught:
        resume(db, task, run, snapshot, filters, clock)
    assert caught.value.code == "checkpoint_scope_changed"
    assert dict(state["requests"]) == requests


def test_expired_collection_window_refuses_complete_reuse(db, site, clock):
    base, _ = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    collect_official(snapshot, filters, task.id, checkpoint=store)
    clock["now"] += timedelta(seconds=3601)
    with pytest.raises(CollectionError) as caught:
        resume(db, task, run, snapshot, filters, clock)
    assert caught.value.code == "checkpoint_expired"


def test_cached_document_observation_uses_step_capture_time_as_authority(db, site, clock):
    base, _ = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    first = collect_official(snapshot, filters, task.id, checkpoint=store)
    original = first.records[0].observed_at
    step = db.scalar(select(OfficialCheckpointStep).where(OfficialCheckpointStep.requested_url.endswith("/job/1")))
    step.parsed_records = [{**value, "observed_at": None} for value in step.parsed_records]
    db.commit()
    clock["now"] += timedelta(minutes=2)
    result = collect_official(snapshot, filters, task.id, checkpoint=resume(db, task, run, snapshot, filters, clock))
    assert next(item for item in result.records if item.title == "工程师1").observed_at == original


def test_cancel_during_http_preserves_completed_step_and_rejects_late_result(db, site, clock):
    base, state = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock)
    def cancel(path):
        if path == "/job/2":
            with session_factory()() as session:
                session.get(TaskRun, task.id).cancel_requested = True
                session.commit()
    state["callback"] = cancel
    with pytest.raises(CollectionError) as caught:
        collect_official(snapshot, filters, task.id, checkpoint=store)
    assert caught.value.code == "cancelled"
    steps = db.scalars(select(OfficialCheckpointStep)).all()
    assert len(steps) == 2 and sum(len(step.parsed_records) for step in steps) == 1
    old = db.get(OfficialCheckpoint, store.id)
    before = json.dumps(old.state, sort_keys=True)
    state["callback"] = None
    replacement = resume(db, task, run, snapshot, filters, clock)
    with pytest.raises(CollectionError) as stale:
        store.save({"collector": "plan", "malicious_old_progress": True})
    assert stale.value.code == "execution_superseded"
    db.refresh(old)
    assert json.dumps(old.state, sort_keys=True) == before
    result = collect_official(snapshot, filters, task.id, checkpoint=replacement)
    assert result.coverage == "complete" and state["requests"]["/job/1"] == 1


def test_tampered_raw_evidence_is_not_silently_reused(db, site, clock):
    base, _ = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    collect_official(snapshot, filters, task.id, checkpoint=store)
    step = db.scalar(select(OfficialCheckpointStep).where(OfficialCheckpointStep.requested_url.endswith("/job/1")))
    (get_settings().storage_dir / step.storage_ref).write_text("changed", encoding="utf-8")
    restored = resume(db, task, run, snapshot, filters, clock)
    with pytest.raises(CollectionError) as caught:
        collect_official(snapshot, filters, task.id, checkpoint=restored)
    assert caught.value.code == "checkpoint_evidence_changed"
    db.expire_all()
    assert db.get(OfficialCheckpoint, store.id).status != "complete"


def test_raw_budget_does_not_commit_unbounded_html_or_complete_status(db, site, clock, monkeypatch):
    base, _ = site
    _, task, _, snapshot, filters, store = seed(db, base + "/list", clock)
    monkeypatch.setattr(get_settings(), "checkpoint_max_raw_bytes", 20)
    with pytest.raises(CollectionError) as caught:
        collect_official(snapshot, filters, task.id, checkpoint=store)
    assert caught.value.code == "checkpoint_raw_budget"
    db.expire_all()
    assert db.get(OfficialCheckpoint, store.id).status != "complete"
    assert db.scalar(select(func.count()).select_from(OfficialCheckpointStep)) == 0


def test_lowered_budget_rejects_existing_cached_evidence_before_resume(db, site, clock, monkeypatch):
    base, state = site
    _, task, run, snapshot, filters, store = seed(db, base + "/list", clock, filters={**FILTERS, "max_details": 1})
    collect_official(snapshot, filters, task.id, checkpoint=store)
    requests = dict(state["requests"])
    monkeypatch.setattr(get_settings(), "checkpoint_max_raw_bytes", 1)
    with pytest.raises(CollectionError) as caught:
        resume(db, task, run, snapshot, filters, clock)
    assert caught.value.code == "checkpoint_raw_budget" and dict(state["requests"]) == requests


def test_observed_at_is_aware_and_not_job_content():
    first = JobDocument(source_job_id="x", title="岗位", url="https://example.com", observed_at=datetime(2026, 10, 5, tzinfo=timezone.utc))
    second = first.model_copy(update={"observed_at": first.observed_at + timedelta(days=1)})
    assert first.content_hash() == second.content_hash()
    with pytest.raises(ValueError):
        JobDocument(source_job_id="x", title="岗位", url="https://example.com", observed_at=datetime(2026, 10, 5))


@pytest.mark.integration
def test_real_celery_worker_resumes_http_failure_then_ingests_once(db, site, tmp_path):
    """A dedicated queue/worker and test DB; production queues are never consumed."""
    from celery import Celery
    import redis
    settings = get_settings()
    broker = settings.redis_url.get_secret_value()
    assert urlparse(broker).hostname in {"127.0.0.1", "localhost", "::1"}
    cache = redis.Redis.from_url(broker)
    assert cache.ping()
    base, state = site
    clock = {"now": datetime.now(timezone.utc)}
    _, task, run, snapshot, filters, _ = seed(db, base + "/list", clock, open_store=False)
    task.status, task.attempt = "queued", 0
    task.config_snapshot = {**filters, "company_name": "检查点队列样本", "sources_snapshot": [snapshot]}
    db.commit()
    state["failures"]["/job/2"] = 1
    queue = "checkpoint-qa-" + uuid.uuid4().hex
    sender = Celery("checkpoint-qa", broker=broker)
    env = {**os.environ, "APP_ENV": "test", "ALLOW_LOCAL_FIXTURE_FETCH": "true",
           "DATABASE_URL": settings.database_url.get_secret_value(), "REDIS_URL": broker,
           "STORAGE_DIR": str(settings.storage_dir), "LLM_API_KEY": "unit-test-not-a-real-key",
           "LLM_BASE_URL": "https://models.example.invalid/v1", "LLM_DAILY_REQUEST_LIMIT": "0"}
    log_path = tmp_path / "checkpoint-worker.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, "-m", "celery", "-A", "app.queue:celery", "worker",
                                    "--pool=threads", "--concurrency=1", "-Q", queue, "--hostname", queue + "@fixture",
                                    "--without-mingle", "--without-gossip", "--without-heartbeat", "--loglevel=INFO"],
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
    def wait_terminal(identifier=None):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            db.expire_all()
            current = db.get(TaskRun, identifier or task.id)
            if current.status not in {"queued", "running"}:
                return current
            if process.poll() is not None:
                raise AssertionError("Isolated Celery worker stopped: " + log_path.read_text()[-2500:])
            time.sleep(0.1)
        raise AssertionError("Existing checkpoint task timed out; worker log: " + log_path.read_text()[-2500:])
    try:
        sender.send_task("app.collect_company", args=[str(task.id)], queue=queue, ignore_result=True)
        assert wait_terminal().status == "failed"
        db.expire_all()
        assert db.scalar(select(func.count()).select_from(OfficialCheckpointStep)) == 2
        assert db.scalar(select(func.count()).select_from(JobObservation)) == 0
        task = db.get(TaskRun, task.id)
        task.status = "queued"
        db.commit()
        sender.send_task("app.collect_company", args=[str(task.id)], queue=queue, ignore_result=True)
        assert wait_terminal().status == "succeeded"
        db.expire_all()
        run = db.get(SourceRun, run.id)
        assert run.counts["observed"] == 3 and run.counts["checkpoint"]["resumed"]
        first_step = db.scalar(select(OfficialCheckpointStep).where(OfficialCheckpointStep.requested_url.endswith("/job/1")))
        first_record = db.scalar(select(JobSourceRecord).where(JobSourceRecord.url.endswith("/job/1")))
        assert first_record.last_seen_at == first_step.captured_at
        assert db.scalar(select(func.count()).select_from(JobObservation)) == 3
        changes = db.scalar(select(func.count()).select_from(JobChange))
        request_counts = dict(state["requests"])
        assert request_counts["/list"] == request_counts["/job/1"] == 1 and request_counts["/job/2"] == 2
        task = db.get(TaskRun, task.id)
        task.status = "queued"
        db.commit()
        sender.send_task("app.collect_company", args=[str(task.id)], queue=queue, ignore_result=True)
        assert wait_terminal().status == "succeeded"
        db.expire_all()
        assert dict(state["requests"]) == request_counts
        assert db.scalar(select(func.count()).select_from(JobObservation)) == 3
        assert db.scalar(select(func.count()).select_from(JobChange)) == changes

        # A separate fresh run must issue HTTP again; a late old worker must not
        # overwrite a replacement attempt that takes ownership during the response.
        fresh = TaskRun(company_id=task.company_id, status="queued", attempt=0,
                        config_snapshot=task.config_snapshot, scope_hash=task.scope_hash)
        db.add(fresh)
        db.flush()
        db.add(CompanyRun(task_run_id=fresh.id, company_id=fresh.company_id, scope_hash=fresh.scope_hash))
        db.commit()
        replaced = Event()
        def take_over(path):
            if path == "/job/2" and not replaced.is_set():
                with session_factory()() as session:
                    current = session.get(TaskRun, fresh.id)
                    current.attempt, current.progress = 2, 73
                    current.summary = {"new_attempt_marker": True}
                    source = session.scalar(select(SourceRun).where(SourceRun.task_id == fresh.id))
                    source.counts = {"new_attempt_marker": True}
                    session.commit()
                replaced.set()
        state["callback"] = take_over
        sender.send_task("app.collect_company", args=[str(fresh.id)], queue=queue, ignore_result=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (replaced.is_set() and not cache.exists("company:" + str(task.company_id))):
            time.sleep(0.1)
        assert replaced.is_set() and not cache.exists("company:" + str(task.company_id))
        db.expire_all()
        fresh = db.get(TaskRun, fresh.id)
        source = db.scalar(select(SourceRun).where(SourceRun.task_id == fresh.id))
        assert fresh.status == "running" and fresh.attempt == 2 and fresh.progress == 73
        assert fresh.summary == source.counts == {"new_attempt_marker": True}
        assert source.status == "running"
        assert state["requests"]["/list"] == state["requests"]["/job/1"] == 2
        assert db.scalar(select(func.count()).select_from(JobObservation)) == 3
        fresh.status, fresh.cancel_requested = "cancelled", True
        db.commit()

        # Exercise the source.partial branch with already-ingested observations.
        # Each attempt adds one detail; earlier raw/observations remain unchanged.
        state["callback"] = None
        budget_task = TaskRun(company_id=task.company_id, status="queued", attempt=0,
                             config_snapshot={**task.config_snapshot, "max_details": 1}, scope_hash="worker-budget-scope")
        db.add(budget_task)
        db.flush()
        db.add(CompanyRun(task_run_id=budget_task.id, company_id=budget_task.company_id, scope_hash=budget_task.scope_hash))
        db.commit()
        before_budget = Counter(state["requests"])
        first_observed = None
        for amount in range(1, 4):
            if amount > 1:
                db.get(TaskRun, budget_task.id).status = "queued"
                db.commit()
            sender.send_task("app.collect_company", args=[str(budget_task.id)], queue=queue, ignore_result=True)
            assert wait_terminal(budget_task.id).status == ("succeeded" if amount == 3 else "partial")
            db.expire_all()
            budget_run = db.scalar(select(SourceRun).where(SourceRun.task_id == budget_task.id))
            assert budget_run.counts["observed"] == amount
            assert db.scalar(select(func.count()).select_from(JobObservation).where(JobObservation.source_run_id == budget_run.id)) == amount
            current = db.scalar(select(JobSourceRecord).where(JobSourceRecord.url.endswith("/job/1")))
            first_observed = first_observed or current.last_seen_at
            assert current.last_seen_at == first_observed
            assert budget_run.counts["suspected_removed"] == 0
        assert Counter(state["requests"]) - before_budget == Counter({"/list": 1, "/list?page=2": 1, "/job/1": 1, "/job/2": 1, "/job/3": 1})
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        with sender.connection_for_write() as connection:
            connection.channel().queue_delete(queue=queue)
        cache.delete("company:" + str(task.company_id))
        sender.close()
