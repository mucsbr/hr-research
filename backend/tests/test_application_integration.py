"""Cross-domain integration against app.main, real sessions and the shared task API."""
import uuid
from collections import Counter

import pytest
from sqlalchemy import select

from app.models import CompanyRun, Job, ModelCall, TaskRun


PRIVATE_READS = [
    "/api/v1/companies", "/api/v1/jobs", "/api/v1/plans", "/api/v1/source-accounts",
    "/api/v1/reports", "/api/v1/users", "/api/v1/audit-events",
    "/api/v1/sentiment/status", "/api/v1/sentiment/subjects", "/api/v1/sentiment/sources",
    "/api/v1/news/status", "/api/v1/news/sources", "/api/v1/assistant/capabilities",
    "/api/v1/notification-channels", "/api/v1/notification-rules",
    "/api/v1/notification-deliveries", "/api/v1/adapter-maintenance/adapters",
    "/api/v1/adapter-maintenance/runs", "/api/v1/settings/recruitment-discovery",
    "/api/v1/retention/policies", "/api/v1/retention/runs",
]


def create_company(client, name):
    response = client.post("/api/v1/companies", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def mark_failed(db, identifier):
    task = db.get(TaskRun, uuid.UUID(identifier))
    task.status, task.error_code = "failed", "test_failure"
    db.commit()
    return task


def test_main_app_registers_all_private_modules_and_enforces_real_sessions(client):
    for path in PRIVATE_READS:
        response = client.get(path)
        assert response.status_code == 401, (path, response.status_code, response.text)
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/readiness").status_code == 200


def test_actual_login_cookie_unlocks_registered_modules_without_model_or_delivery(signed_client, db):
    from app.notification_models import NotificationDelivery
    for path in PRIVATE_READS:
        response = signed_client.get(path)
        assert response.status_code == 200, (path, response.status_code, response.text)
    assert not db.scalars(select(ModelCall)).all()
    assert not db.scalars(select(NotificationDelivery)).all()


def test_expired_session_is_denied_but_new_login_preserves_retention_evidence(signed_client, db):
    from datetime import timedelta
    from app.config import get_settings
    from app.models import UserSession, now

    expired = db.scalar(select(UserSession))
    expired_id = expired.id
    expired.expires_at = now() - timedelta(days=1)
    db.commit()
    assert signed_client.get("/api/v1/companies").status_code == 401
    settings = get_settings()
    response = signed_client.post("/api/v1/auth/login", json={
        "username": settings.admin_username, "password": settings.admin_password.get_secret_value()})
    assert response.status_code == 200
    assert signed_client.get("/api/v1/companies").status_code == 200
    db.expire_all()
    assert db.get(UserSession, expired_id) is not None


@pytest.mark.parametrize("path,payload", [
    ("/api/v1/sentiment/subjects", {"name": "恶意跨站写入"}),
    ("/api/v1/news/sources", {"name": "恶意跨站来源", "url": "https://example.invalid/feed"}),
    ("/api/v1/assistant/messages", {"message": "添加公司：恶意公司"}),
    ("/api/v1/adapter-maintenance/register", {}),
    ("/api/v1/retention/preview", {}),
])
def test_main_origin_guard_applies_to_new_modules(signed_client, path, payload):
    response = signed_client.post(path, json=payload, headers={"Origin": "https://foreign.example.invalid"})
    assert response.status_code == 403


def test_main_validation_handler_does_not_echo_action_tokens_or_webhook_secrets(signed_client):
    fake_secret = "not-a-real-webhook-token-" * 100
    response = signed_client.post("/api/v1/notification-channels", json={
        "name": "校验测试", "kind": "feishu", "url": "https://invalid.example.invalid/" + fake_secret})
    assert response.status_code == 422
    assert fake_secret not in response.text
    assert all("input" not in error for error in response.json()["detail"])
    action = "not-a-real-action-token-" * 1000
    response = signed_client.post("/api/v1/assistant/actions/execute", json={"action_token": action})
    assert response.status_code == 422 and action not in response.text


def test_regular_user_session_cannot_modify_administrative_modules(signed_client):
    response = signed_client.post("/api/v1/users", json={
        "username": "integration_reader", "password": "integration-reader-test-password", "role": "user"})
    assert response.status_code == 201
    signed_client.post("/api/v1/auth/logout")
    response = signed_client.post("/api/v1/auth/login", json={
        "username": "integration_reader", "password": "integration-reader-test-password"})
    assert response.status_code == 200
    assert signed_client.get("/api/v1/companies").status_code == 200
    assert signed_client.get("/api/v1/news/sources").status_code == 200
    assert signed_client.post("/api/v1/news/sources", json={
        "name": "不应创建", "url": "https://example.invalid/feed"}).status_code == 403
    assert signed_client.post("/api/v1/adapter-maintenance/register", json={}).status_code == 403
    assert signed_client.get("/api/v1/users").status_code == 403
    assert signed_client.post("/api/v1/source-accounts", json={"kind": "boss", "name": "不应创建"}).status_code == 403
    assert signed_client.get("/api/v1/retention/policies").json()["can_manage"] is False
    assert signed_client.get("/api/v1/retention/runs").status_code == 200
    assert signed_client.post("/api/v1/retention/preview", json={}).status_code == 403
    assert signed_client.post("/api/v1/retention/runs", json={
        "preview_id": str(uuid.uuid4()), "idempotency_key": "reader-cannot-clean"}).status_code == 403


def test_task_polling_list_is_small_but_detail_preserves_frozen_evidence(signed_client):
    first, second = create_company(signed_client, "集成甲"), create_company(signed_client, "集成乙")
    response = signed_client.post("/api/v1/recruitment/comparisons", json={
        "company_ids": [first["id"], second["id"]], "days": 30})
    identifier = response.json()["task_id"]
    tasks = signed_client.get("/api/v1/tasks").json()
    listed = next(task for task in tasks if task["id"] == identifier)
    assert "metrics_snapshot" not in listed["config_snapshot"]
    detail = signed_client.get("/api/v1/tasks/" + identifier).json()
    assert len(detail["config_snapshot"]["metrics_snapshot"]) == 2


def test_shared_task_detail_and_retry_preserve_sentiment_domain(signed_client, db):
    subject = signed_client.post("/api/v1/sentiment/subjects", json={"name": "集成公司"}).json()
    source = signed_client.post("/api/v1/sentiment/sources", json={
        "subject_id": subject["id"], "name": "公开页", "kind": "public_web",
        "entry_urls": ["https://example.invalid/post"],
    })
    assert source.status_code == 201
    created = signed_client.post("/api/v1/sentiment/runs", json={"subject_id": subject["id"]})
    assert created.status_code == 202
    original = mark_failed(db, created.json()["task_id"])
    detail = signed_client.get("/api/v1/tasks/" + str(original.id)).json()
    assert detail["domain"] == "sentiment" and detail["sources"] == [] and detail["events"]
    retried = signed_client.post(f"/api/v1/tasks/{original.id}/retry")
    assert retried.status_code == 202
    current = db.get(TaskRun, uuid.UUID(retried.json()["task_id"]))
    assert current.kind == "sentiment_collection" and current.domain == "sentiment" and current.company_id is None
    assert current.config_snapshot["subject"]["id"] == original.config_snapshot["subject"]["id"]
    assert {s["id"] for s in current.config_snapshot["sources"]} == {s["id"] for s in original.config_snapshot["sources"]}
    assert current.config_snapshot["selected_source_ids"] == [s["id"] for s in original.config_snapshot["sources"]]
    for key in ("time_from", "time_to", "max_pages", "render"):
        assert current.config_snapshot[key] == original.config_snapshot[key]
    assert not db.scalars(select(CompanyRun)).all()


def test_shared_task_retry_cannot_turn_sentiment_import_into_a_collection(signed_client, db):
    subject = signed_client.post("/api/v1/sentiment/subjects", json={"name": "集成公司"}).json()
    result = signed_client.post("/api/v1/sentiment/imports", json={"subject_id": subject["id"], "items": [{
        "url": "https://example.invalid/post", "content": "集成公司提供的客户服务收到了一条公开反馈。"}]})
    assert result.status_code == 201
    response = signed_client.post("/api/v1/tasks/" + result.json()["task_id"] + "/retry")
    assert response.status_code == 422
    assert len(db.scalars(select(TaskRun)).all()) == 1


def test_shared_task_detail_and_retry_preserve_news_domain(signed_client, db):
    source = signed_client.post("/api/v1/news/sources", json={
        "name": "配置的HR来源", "kind": "feed", "url": "https://example.invalid/feed",
        "regions": ["上海"], "keywords": ["劳动关系"],
    })
    assert source.status_code == 201
    created = signed_client.post("/api/v1/news/runs", json={"source_ids": [source.json()["id"]]})
    assert created.status_code == 202
    original = mark_failed(db, created.json()["task_id"])
    detail = signed_client.get("/api/v1/tasks/" + str(original.id)).json()
    assert detail["domain"] == "news" and detail["sources"] == []
    retried = signed_client.post(f"/api/v1/tasks/{original.id}/retry")
    assert retried.status_code == 202
    current = db.get(TaskRun, uuid.UUID(retried.json()["task_id"]))
    assert current.kind == "news_collection" and current.company_id is None
    assert current.config_snapshot["sources"][0]["regions"] == ["上海"]
    assert not db.scalars(select(CompanyRun)).all()


def test_shared_task_retry_preserves_comparison_as_independent_statistics(signed_client, db):
    first, second = create_company(signed_client, "集成甲"), create_company(signed_client, "集成乙")
    created = signed_client.post("/api/v1/recruitment/comparisons", json={
        "company_ids": [first["id"], second["id"]], "days": 7})
    assert created.status_code == 202
    original = mark_failed(db, created.json()["task_id"])
    retried = signed_client.post(f"/api/v1/tasks/{original.id}/retry")
    assert retried.status_code == 202
    current = db.get(TaskRun, uuid.UUID(retried.json()["task_id"]))
    assert current.kind == "company_comparison" and current.company_id is None
    assert current.config_snapshot["days"] == 7 and len(current.config_snapshot["metrics_snapshot"]) == 2
    assert current.config_snapshot["as_of"] != original.config_snapshot["as_of"]
    assert not db.scalars(select(CompanyRun)).all() and not db.scalars(select(ModelCall)).all()


def test_shared_task_retry_preserves_analysis_and_collection_task_kinds(signed_client, db):
    company = create_company(signed_client, "集成分析公司")
    company_id = uuid.UUID(company["id"])
    db.add(Job(company_id=company_id, title="工程师"))
    analysis = TaskRun(company_id=company_id, kind="company_analysis", status="failed")
    collection = TaskRun(company_id=company_id, kind="company_collection", status="failed", config_snapshot={"cities": ["上海"]})
    db.add_all([analysis, collection])
    db.commit()
    result = signed_client.post(f"/api/v1/tasks/{analysis.id}/retry")
    assert result.status_code == 202
    new_analysis = db.get(TaskRun, uuid.UUID(result.json()["task_id"]))
    assert new_analysis.kind == "company_analysis" and new_analysis.company_id == company_id
    result = signed_client.post(f"/api/v1/tasks/{collection.id}/retry")
    assert result.status_code == 202
    new_collection = db.scalar(select(TaskRun).where(TaskRun.batch_id == uuid.UUID(result.json()["batch_id"])))
    assert new_collection.kind == "company_collection" and new_collection.company_id == company_id
    assert new_collection.config_snapshot["cities"] == ["上海"]


@pytest.mark.parametrize("domain,kind", [("recruitment", "company_comparison"),
                                          ("sentiment", "sentiment_collection"), ("news", "news_collection")])
def test_shared_cancel_and_running_retry_rules_apply_to_every_domain(signed_client, db, domain, kind):
    task = TaskRun(domain=domain, kind=kind)
    db.add(task)
    db.commit()
    assert signed_client.post(f"/api/v1/tasks/{task.id}/retry").status_code == 409
    response = signed_client.post(f"/api/v1/tasks/{task.id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert response.json()["cancel_requested"] is True


def test_browser_worker_endpoint_is_not_unlocked_by_user_login(signed_client):
    from app.config import get_settings
    request = {"task_id": str(uuid.uuid4()), "binding_id": str(uuid.uuid4()), "filters": {}, "expected_attempt": 1}
    assert signed_client.post("/api/internal/browser/collect", json=request).status_code == 403
    assert signed_client.post("/api/internal/browser/collect", json=request,
                              headers={"X-HR-Worker-Token": "not-valid"}).status_code == 403
    response = signed_client.post("/api/internal/browser/collect", json=request,
                                   headers={"X-HR-Worker-Token": get_settings().app_secret.get_secret_value()})
    assert response.status_code == 404


def test_main_routes_have_unique_method_path_and_celery_tasks_are_registered(client):
    from app.main import app
    from app.queue import celery
    from app import workers
    assert workers.dispatch_pending.name == "app.dispatch_pending"
    routes = [(method, route.path) for route in app.routes for method in getattr(route, "methods", [])]
    assert all(count == 1 for count in Counter(routes).values())
    registered = {"app.collect_company", "app.analyze_company", "app.compare_companies", "app.run_sentiment",
                  "app.run_news", "app.deliver_notifications", "app.dispatch_pending", "app.run_schedules",
                  "app.dispatch_maintenance", "app.check_adapter", "app.run_retention"}
    assert registered <= set(celery.tasks)


def test_dispatcher_routes_each_domain_without_using_live_broker(db, monkeypatch):
    from app import workers
    captured = []
    monkeypatch.setattr(workers, "redis_client", lambda: type("NoRedis", (), {"exists": lambda *_: False})())
    mapping = {"collect_company": "recruitment", "analyze_company": "analysis", "compare_companies": "analysis",
               "run_sentiment": "sentiment", "run_news": "news"}
    for name in mapping:
        def capture(*, args, queue, _name=name):
            captured.append((_name, args[0], queue))
        monkeypatch.setattr(getattr(workers, name), "apply_async", capture)
    tasks = [TaskRun(domain=domain, kind=kind) for domain, kind in [
        ("recruitment", "company_collection"), ("recruitment", "company_analysis"),
        ("recruitment", "company_comparison"), ("sentiment", "sentiment_collection"),
        ("sentiment", "sentiment_analysis"), ("sentiment", "sentiment_bettafish"), ("news", "news_collection")]]
    cancelled = TaskRun(domain="news", kind="news_collection", cancel_requested=True)
    db.add_all([*tasks, cancelled])
    db.commit()
    workers.dispatch_pending.run()
    assert len(captured) == len(tasks)
    assert {identifier for _, identifier, _ in captured} == {str(task.id) for task in tasks}
    assert all(queue == mapping[name] for name, _, queue in captured)
