"""Real PostgreSQL + Redis leases + worker calls + localhost ASGI HTTP.

Only the browser adapter is replaced by a bounded local fixture reader. Company
and account locks are real Redis locks; no fake lock can make these tests pass.
"""
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import socket
from threading import Barrier, Event, Lock, Thread
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

import httpx
from pydantic import SecretStr
import pytest
import redis
from sqlalchemy import select
import uvicorn

from app.collection import CollectionResult, JobDocument
from app.config import get_settings
from app.db import session_factory
from app.ingest import ingest
from app.llm import ModelGateway
from app.models import (
    Company, Job, JobChange, JobSourceRecord, ModelCall, Report, SourceAccount, SourceBinding,
    SourceRun, Subject, TaskRun,
)
from app.notification_models import NotificationDelivery
from app.schemas import RunInput
from app.tasks import create_analysis, create_batch

pytestmark = pytest.mark.integration


@pytest.fixture
def real_redis(engine, monkeypatch):
    settings = get_settings()
    parsed = urlsplit(settings.redis_url.get_secret_value())
    if parsed.scheme not in {"redis", "rediss"}:
        pytest.fail("Runtime integration needs the configured project Redis")
    # A separate logical database plus UUID-only keys; never FLUSHDB or touch production keys.
    dedicated = urlunsplit((parsed.scheme, parsed.netloc, "/14", parsed.query, ""))
    client = redis.Redis.from_url(dedicated, socket_connect_timeout=2, socket_timeout=2)
    try:
        assert client.ping()
    except redis.RedisError:
        pytest.fail("Real project Redis is unavailable; concurrency proof cannot use a fake lock")
    monkeypatch.setattr(settings, "redis_url", SecretStr(dedicated))
    owned_keys = set()
    yield client, owned_keys
    if owned_keys:
        client.delete(*owned_keys)
    client.close()


@pytest.fixture
def runtime_http(db, monkeypatch):
    from app.main import app
    prefix = "/_qa_runtime_" + uuid.uuid4().hex
    datasets = {}

    def fixture_jobs(company_id: str):
        return datasets[company_id]

    app.add_api_route(prefix + "/jobs/{company_id}", fixture_jobs, methods=["GET"])
    fixture_route = app.router.routes[-1]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                          lifespan="on", timeout_graceful_shutdown=3))
    thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        Event().wait(0.02)
    assert server.started, "The actual main ASGI app did not become ready"
    monkeypatch.setattr(get_settings(), "browser_service_url", base_url)
    try:
        with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
            assert client.get("/api/v1/health").status_code == 200
            login = client.post("/api/v1/auth/login", json={
                "username": get_settings().admin_username,
                "password": get_settings().admin_password.get_secret_value(),
            })
            assert login.status_code == 200
            yield base_url, prefix, datasets, client
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        listener.close()
        app.router.routes.remove(fixture_route)
        assert not thread.is_alive(), "Local QA HTTP server failed to stop"


def companies_and_accounts(db, names, shared=True):
    accounts = [SourceAccount(kind="boss", name="QA账号甲", health="saved"),
                SourceAccount(kind="boss", name="QA账号乙", health="saved")]
    db.add_all(accounts)
    db.flush()
    companies, bindings = [], []
    for index, name in enumerate(names):
        subject = Subject(name=name)
        db.add(subject)
        db.flush()
        company = Company(subject_id=subject.id, name=name)
        db.add(company)
        db.flush()
        account = accounts[0] if shared and index < 2 else accounts[min(index, 1)]
        binding = SourceBinding(company_id=company.id, kind="boss", account_id=account.id,
                                entry_url=f"https://www.zhipin.com/gongsi/qa-{company.id}.html",
                                external_company_id=str(company.id))
        db.add(binding)
        companies.append(company)
        bindings.append(binding)
    db.commit()
    return companies, bindings, accounts


def install_fixture_browser(monkeypatch, runtime_http, real_redis, companies, *, expected_parallel):
    base_url, prefix, datasets, _ = runtime_http
    redis_client, _ = real_redis
    for company in companies:
        datasets[str(company.id)] = {"jobs": [{
            "source_job_id": "same-id-across-companies", "title": company.name + " · 软件工程师",
            "url": f"https://www.zhipin.com/job_detail/qa-{company.id}.html",
            "locations": ["上海"], "description": company.name + "的独立岗位证据，仅属于这一家公司。",
            "recruiting_entity": company.name,
        }]}
    mutex, release, parallel_entered = Lock(), Event(), Event()
    state = {"active": Counter(), "max_by_account": Counter(), "max_total": 0, "calls": [], "entry_errors": []}

    def collect(account_id, kind, binding, identity, filters, progress=None, *, descriptor, runtime_scope):
        # The real internal route now freezes its adapter and passes explicit execution scope.
        # Keep this fixture on the native contract; it must not pretend to validate remote code.
        assert descriptor.kind == kind and descriptor.program is None
        assert set(runtime_scope) == {"task_id", "source_run_id", "expected_attempt"}
        with session_factory()() as current:
            task = current.get(TaskRun, runtime_scope["task_id"])
            source = current.get(SourceRun, runtime_scope["source_run_id"])
            assert task.company_id == uuid.UUID(str(binding["company_id"]))
            assert task.status == "running" and task.attempt == runtime_scope["expected_attempt"]
            assert source.task_id == task.id and source.binding_id == uuid.UUID(str(binding["id"]))
            assert source.status == "running"
            frozen = task.config_snapshot["adapter_versions"][str(binding["id"])]
            assert frozen == {"kind": kind, "release_id": descriptor.release_id,
                              "generation": descriptor.generation, "fingerprint": descriptor.fingerprint}
        # The unchanged internal HTTP handler must already own the actual Redis lease.
        assert redis_client.exists("account:" + str(account_id)) == 1
        assert redis_client.exists("company:" + str(binding["company_id"])) == 1
        with mutex:
            state["active"][str(account_id)] += 1
            state["max_by_account"][str(account_id)] = max(state["max_by_account"][str(account_id)], state["active"][str(account_id)])
            state["max_total"] = max(state["max_total"], sum(state["active"].values()))
            state["calls"].append({"company_id": str(binding["company_id"]), "identity": dict(identity), "account_id": str(account_id)})
            if sum(state["active"].values()) >= expected_parallel:
                parallel_entered.set()
        try:
            assert release.wait(timeout=10), "QA did not release the browser fixture"
            if progress:
                progress("读取本地隔离浏览器测试资料")
            with httpx.Client(timeout=5, trust_env=False) as client:
                response = client.get(base_url + prefix + "/jobs/" + str(binding["company_id"]))
                response.raise_for_status()
                payload = response.json()
            return CollectionResult(records=[JobDocument.model_validate(item) for item in payload["jobs"]],
                                    documents=[(binding["entry_url"], json.dumps(payload, ensure_ascii=False), "application/json")],
                                    coverage="complete", pages=1, adapter_fingerprint=descriptor.fingerprint)
        finally:
            with mutex:
                state["active"][str(account_id)] -= 1

    def protocol_entry(*args, **kwargs):
        try:
            return collect(*args, **kwargs)
        except TypeError as exc:
            with mutex:
                state["entry_errors"].append(str(exc))
            raise

    monkeypatch.setattr("app.browser_internal.collect_platform", protocol_entry)
    return state, release, parallel_entered


def test_three_companies_real_worker_http_and_redis_account_serialization(db, monkeypatch, real_redis, runtime_http):
    from app import workers
    companies, bindings, accounts = companies_and_accounts(db, ["并发甲公司", "并发乙公司", "并发丙公司"])
    redis_client, keys = real_redis
    keys.update("company:" + str(company.id) for company in companies)
    keys.update("account:" + str(account.id) for account in accounts)
    batch, _ = create_batch(db, RunInput(company_ids=[company.id for company in companies]))
    tasks = db.scalars(select(TaskRun).where(TaskRun.batch_id == batch.id)).all()
    state, release, entered = install_fixture_browser(monkeypatch, runtime_http, real_redis, companies, expected_parallel=2)
    start = Barrier(3)

    def run(task_id):
        start.wait(timeout=5)
        workers.collect_company.run(str(task_id))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, task.id) for task in tasks]
        try:
            assert entered.wait(timeout=8), "Different source accounts did not execute concurrently: " + repr(state["entry_errors"])
            completed, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert len(completed) == 1, "Exactly one shared-account contender should return waiting_account"
            for future in completed:
                future.result()
            with session_factory()() as current:
                waiting = current.scalars(select(TaskRun).where(TaskRun.batch_id == batch.id,
                                                               TaskRun.status == "waiting_account")).all()
                assert len(waiting) == 1
                waiting_id = waiting[0].id
            assert state["max_total"] == 2
            assert all(value == 1 for value in state["max_by_account"].values())
            assert redis_client.exists("account:" + str(accounts[0].id)) == 1
            assert redis_client.exists("account:" + str(accounts[1].id)) == 1
        finally:
            release.set()
        for future in futures:
            future.result(timeout=12)

    # Resume through the actual dispatcher decision; only broker enqueue is recorded locally.
    queued = []
    monkeypatch.setattr(workers.collect_company, "apply_async", lambda **kwargs: queued.append(kwargs))
    workers.dispatch_pending.run()
    assert any(str(waiting_id) in entry["args"] for entry in queued)
    workers.collect_company.run(str(waiting_id))
    db.expire_all()
    assert all(db.get(TaskRun, task.id).status == "succeeded" for task in tasks)
    records = db.execute(select(JobSourceRecord, Job).join(Job, JobSourceRecord.job_id == Job.id)).all()
    assert len(records) == 3 and len({record.job_id for record, _ in records}) == 3
    expected = {str(company.id): company.name for company in companies}
    for record, job in records:
        assert record.recruiting_entity == expected[str(job.company_id)]
        assert record.data["description"].startswith(expected[str(job.company_id)])
    for call in state["calls"]:
        assert call["identity"]["name"] == expected[call["company_id"]]
    assert len(state["calls"]) == 3 and all(value == 1 for value in state["max_by_account"].values())
    assert state["entry_errors"] == []
    assert len(db.scalars(select(SourceRun)).all()) == 3
    assert not db.scalars(select(ModelCall)).all() and not db.scalars(select(NotificationDelivery)).all()
    assert not any(redis_client.exists(key) for key in keys)
    api = runtime_http[3]
    for company in companies:
        payload = api.get("/api/v1/jobs", params={"company_id": str(company.id)}).json()
        assert payload["total"] == 1 and payload["items"][0]["title"].startswith(company.name)

    # Continue through the real analysis worker/graph, but keep every model request
    # inside MockTransport. The barrier proves simultaneous independent contexts.
    analysis_tasks = [create_analysis(db, company.id)[0] for company in companies]
    model_barrier, prompt_lock, prompts = Barrier(3), Lock(), []
    def model_response(request):
        incoming = json.loads(request.content)
        material = json.loads(incoming["messages"][-1]["content"])
        company_name = material["company"]
        assert company_name in expected.values()
        assert all(other == company_name or other not in incoming["messages"][-1]["content"]
                   for other in expected.values())
        with prompt_lock:
            prompts.append(material)
        model_barrier.wait(timeout=5)
        content = {"summary": company_name + "的独立采集样本", "observations": [{
            "text": "只使用本公司证据", "job_ids": [material["evidence"][0]["job_id"]]}],
            "limitations": ["仅为本地fixture，不代表真实招聘情况"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)},
                                                       "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 40}})
    monkeypatch.setattr("app.agents.ModelGateway", lambda: ModelGateway(httpx.MockTransport(model_response)))
    monkeypatch.setattr(get_settings(), "llm_daily_request_limit", 3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        reports = [pool.submit(workers.analyze_company.run, str(task.id)) for task in analysis_tasks]
        for result in reports:
            result.result(timeout=12)
    db.expire_all()
    assert len(prompts) == 3 and {entry["company"] for entry in prompts} == set(expected.values())
    assert all(db.get(TaskRun, task.id).status == "succeeded" for task in analysis_tasks)
    for report in db.scalars(select(Report)):
        analysis_task = db.get(TaskRun, report.task_id)
        assert all(db.get(Job, uuid.UUID(evidence["job_id"])).company_id == analysis_task.company_id
                   for evidence in report.evidence)
    assert len(db.scalars(select(ModelCall)).all()) == 3  # all MockTransport, zero paid requests
    assert not db.scalars(select(NotificationDelivery)).all()


def test_three_company_plan_one_terminal_failure_does_not_block_others(db, monkeypatch, real_redis, runtime_http):
    from fastapi.responses import JSONResponse
    from app import workers

    companies, bindings, accounts = companies_and_accounts(
        db, ["成功独立甲公司", "失败独立乙公司", "成功独立丙公司"], shared=False)
    third_account = SourceAccount(kind="boss", name="QA账号丙", health="saved")
    db.add(third_account)
    db.flush()
    bindings[2].account_id = third_account.id
    db.commit()
    accounts.append(third_account)
    redis_client, keys = real_redis
    keys.update("company:" + str(company.id) for company in companies)
    keys.update("account:" + str(account.id) for account in accounts)

    # Use the actual user-facing plan and run endpoints, with a separate task per company.
    api = runtime_http[3]
    plan = api.post("/api/v1/plans", json={
        "name": "三家公司失败隔离验收", "company_ids": [str(company.id) for company in companies],
        "filters": {"max_pages": 1, "max_details": 1},
    })
    assert plan.status_code == 201
    started = api.post(f"/api/v1/plans/{plan.json()['id']}/run")
    assert started.status_code == 202
    batch_id = uuid.UUID(started.json()["batch_id"])
    tasks = db.scalars(select(TaskRun).where(TaskRun.batch_id == batch_id)).all()
    assert len(tasks) == 3 and {task.company_id for task in tasks} == {company.id for company in companies}
    state, release, entered = install_fixture_browser(
        monkeypatch, runtime_http, real_redis, companies, expected_parallel=3)
    failed_company = companies[1]
    # This is an actual localhost HTTP 503, propagated through the unchanged
    # internal browser route and worker, not an assigned TaskRun failure status.
    runtime_http[2][str(failed_company.id)] = JSONResponse({"error": "QA source unavailable"}, status_code=503)
    start = Barrier(3)

    def run(task_id):
        start.wait(timeout=5)
        workers.collect_company.run(str(task_id))

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, task.id) for task in tasks]
        try:
            assert entered.wait(timeout=8), "The three independent company workers did not overlap"
            assert state["max_total"] == 3
            assert all(value == 1 for value in state["max_by_account"].values())
        finally:
            release.set()
        for future in futures:
            future.result(timeout=12)

    db.expire_all()
    batch = api.get(f"/api/v1/batches/{batch_id}").json()
    assert batch["status"] == "completed_with_issues"
    assert batch["counts"] == {"succeeded": 2, "failed": 1}
    assert len(state["calls"]) == 3
    assert state["entry_errors"] == []
    for task in tasks:
        detail = api.get(f"/api/v1/tasks/{task.id}").json()
        failed = task.company_id == failed_company.id
        assert detail["status"] == ("failed" if failed else "succeeded")
        assert len(detail["sources"]) == 1
        assert detail["sources"][0]["status"] == ("failed" if failed else "succeeded")
        if failed:
            assert detail["sources"][0]["error_code"] == "browser_collection_failed"
        jobs = api.get("/api/v1/jobs", params={"company_id": str(task.company_id)}).json()
        assert jobs["total"] == (0 if failed else 1)
        if not failed:
            assert jobs["items"][0]["title"].startswith(task.config_snapshot["company_identity"]["name"])
    records = db.execute(select(JobSourceRecord, Job).join(Job, JobSourceRecord.job_id == Job.id)).all()
    assert len(records) == 2 and len({record.job_id for record, _ in records}) == 2
    assert all(job.company_id != failed_company.id for _, job in records)
    assert len(db.scalars(select(SourceRun)).all()) == 3
    assert not db.scalars(select(ModelCall)).all() and not db.scalars(select(NotificationDelivery)).all()
    assert not any(redis_client.exists(key) for key in keys)


def test_same_company_real_redis_lease_prevents_parallel_reentry(db, monkeypatch, real_redis, runtime_http):
    from app import workers
    companies, _, accounts = companies_and_accounts(db, ["同公司租约测试"])
    company = companies[0]
    redis_client, keys = real_redis
    keys.add("company:" + str(company.id))
    keys.update("account:" + str(account.id) for account in accounts)
    first, _ = create_batch(db, RunInput(company_ids=[company.id], idempotency_key="runtime-first:" + str(company.id)))
    second, _ = create_batch(db, RunInput(company_ids=[company.id], idempotency_key="runtime-second:" + str(company.id)))
    assert first.id != second.id
    first_task = db.scalar(select(TaskRun).where(TaskRun.batch_id == first.id))
    second_task = db.scalar(select(TaskRun).where(TaskRun.batch_id == second.id))
    state, release, entered = install_fixture_browser(monkeypatch, runtime_http, real_redis, companies, expected_parallel=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(workers.collect_company.run, str(first_task.id))
        try:
            assert entered.wait(timeout=8)
            lease_token = redis_client.get("company:" + str(company.id))
            other = pool.submit(workers.collect_company.run, str(second_task.id))
            other.result(timeout=5)
            assert redis_client.get("company:" + str(company.id)) == lease_token
            db.expire_all()
            assert db.get(TaskRun, second_task.id).status == "queued"
            assert db.get(TaskRun, second_task.id).attempt == 0
            assert len(state["calls"]) == 1
        finally:
            release.set()
        running.result(timeout=12)
    workers.collect_company.run(str(second_task.id))
    db.expire_all()
    assert db.get(TaskRun, second_task.id).status == "succeeded"
    assert len(db.scalars(select(JobSourceRecord)).all()) == 1
    assert len(state["calls"]) == 2 and state["max_total"] == 1
    assert state["entry_errors"] == []
    assert not any(redis_client.exists(key) for key in keys)


def test_one_merged_source_disappears_other_still_present_in_real_job_api(signed_client, db):
    response = signed_client.post("/api/v1/companies", json={"name": "跨来源展示测试"})
    company = db.get(Company, uuid.UUID(response.json()["id"]))
    careers = SourceBinding(company_id=company.id, kind="careers", entry_url="https://careers.example.invalid/jobs")
    boss = SourceBinding(company_id=company.id, kind="boss", entry_url="https://www.zhipin.com/gongsi/qa.html")
    db.add_all([careers, boss])
    db.commit()
    description = "负责企业平台研发和稳定性建设，参与系统架构设计与性能优化，维护接口与数据流程，和产品团队协作完成迭代，具备扎实的软件工程基础。"

    def observe(binding, present):
        task = TaskRun(company_id=company.id, config_snapshot={})
        db.add(task)
        db.flush()
        run = SourceRun(task_id=task.id, binding_id=binding.id, scope_hash="stable-source-scope")
        db.add(run)
        db.flush()
        docs = [JobDocument(source_job_id="shared-role", title="平台研发工程师", url=binding.entry_url + "/1",
                            locations=["上海"], recruiting_entity=company.name, description=description)] if present else []
        ingest(db, task, binding, run, CollectionResult(records=docs, coverage="complete", pages=1))
        db.commit()

    observe(careers, True)
    observe(boss, True)
    left = db.scalar(select(JobSourceRecord).where(JobSourceRecord.binding_id == careers.id))
    target_id = left.job_id
    proposals = signed_client.post(f"/api/v1/companies/{company.id}/job-matches").json()["candidates"]
    assert len(proposals) == 1
    merged = signed_client.post(f"/api/v1/job-matches/{proposals[0]['id']}/merge", json={
        "target_job_id": str(target_id), "reason": "QA验证同岗位跨来源状态分别保留"})
    assert merged.status_code == 200
    observe(careers, False)
    observe(boss, True)
    observe(careers, False)
    observe(boss, True)
    detail = signed_client.get(f"/api/v1/jobs/{target_id}").json()
    assert detail["status"] == "observed"
    by_source = {source["binding_id"]: source for source in detail["sources"]}
    assert by_source[str(careers.id)]["status"] == "suspected_removed"
    assert by_source[str(boss.id)]["status"] == "observed"
    listing = signed_client.get("/api/v1/jobs", params={"company_id": str(company.id)}).json()
    assert listing["total"] == 1 and listing["items"][0]["status"] == "observed"
    assert len(listing["items"][0]["sources"]) == 2
    changes = db.scalars(select(JobChange).where(JobChange.type == "suspected_removed")).all()
    assert len(changes) == 1 and changes[0].record_id == left.id
