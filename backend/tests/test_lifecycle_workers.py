"""Public commands and registered Worker tasks complete the real durable workflows."""
from datetime import date, datetime, timedelta, timezone
from functools import partial
import hashlib
import json
import uuid

import httpx
from sqlalchemy import func, select

from app.config import get_settings
from app.models import ModelCall, RawDocument, TaskRun, now


def test_public_retention_request_reaches_registered_worker_once(signed_client, db, monkeypatch):
    from app import workers  # noqa: F401
    from app.queue import celery
    from app.retention_models import RetentionPolicy, RetentionRun

    at = now()
    policies = signed_client.get("/api/v1/retention/policies")
    assert policies.status_code == 200
    policy = db.scalar(select(RetentionPolicy).where(RetentionPolicy.category == "raw_documents"))
    policy.retention_days, policy.grace_until = 1, at - timedelta(days=1)
    task = TaskRun(status="succeeded", created_at=at - timedelta(days=10), finished_at=at - timedelta(days=9))
    db.add(task)
    db.flush()
    content = b"isolated worker retention evidence"
    sha = hashlib.sha256(content).hexdigest()
    relative = f"tasks/{task.id}/{sha}.txt"
    path = get_settings().storage_dir / relative
    assert "pytest-" in str(path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    raw = RawDocument(task_id=task.id, url="https://example.invalid/source", sha256=sha,
                      storage_ref=relative, created_at=at - timedelta(days=5))
    db.add(raw)
    db.commit()
    response = signed_client.post("/api/v1/retention/preview", json={"categories": ["raw_documents"]})
    assert response.status_code == 200 and response.json()["eligible_count"] == 1
    outgoing = []
    monkeypatch.setattr(celery, "send_task", lambda name, **options: outgoing.append((name, options)))
    payload = {"preview_id": response.json()["id"], "idempotency_key": "worker-integration-" + str(uuid.uuid4())}
    submitted = signed_client.post("/api/v1/retention/runs", json=payload)
    assert submitted.status_code == 202 and submitted.json()["status"] == "queued"
    assert path.exists() and len(outgoing) == 1
    name, options = outgoing[0]
    assert options["queue"] == "maintenance" and options["kwargs"] == {"run_id": submitted.json()["id"]}
    result = celery.tasks[name].run(**options["kwargs"])
    assert result["status"] == "succeeded" and result["summary"]["files_deleted"] == 1
    assert not path.exists()
    db.expire_all()
    assert db.get(RawDocument, raw.id).sha256 == sha and db.get(RawDocument, raw.id).cleared_at
    assert db.get(TaskRun, task.id).status == "succeeded"
    again = signed_client.post("/api/v1/retention/runs", json=payload)
    assert again.status_code == 202 and again.json()["id"] == submitted.json()["id"]
    assert len(outgoing) == 1
    assert celery.tasks[name].run(**options["kwargs"])["summary"] == result["summary"]
    assert db.scalar(select(func.count()).select_from(RetentionRun)) == 1
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_notification_worker_materializes_and_delivers_due_policy_without_model(db, monkeypatch):
    from app import notifications, workers
    from app.news_models import NewsSource, NewsSourceRun
    from app.news_service import NewsDocument, ingest_news
    from app.notification_models import NewsPolicyReminder, NotificationChannel, NotificationDelivery, NotificationRule
    from app.serialization import row

    at = [datetime(2032, 1, 1, 1, 0, tzinfo=timezone.utc)]
    for module in ("app.news_service", "app.news_policy_reminders", "app.notifications"):
        monkeypatch.setattr(module + ".now", lambda: at[0])
    channel = NotificationChannel(name="Worker QA", kind="webhook", enabled=True,
        destination_hint="hooks.example.invalid", encrypted_config=notifications.encrypt_config({
            "url": "https://hooks.example.invalid/receiver"}))
    source = NewsSource(name="Worker official policy fixture", kind="web", url="https://hr.example.invalid/",
                        source_type="official", regions=["上海"], topics=["用工政策"])
    task = TaskRun(domain="news", kind="news_collection", status="succeeded", finished_at=at[0])
    db.add_all([channel, source, task])
    db.flush()
    db.add(NotificationRule(name="施行提醒", channel_id=channel.id, event_types=["news.policy_effective"],
                            enabled=True, news_filters={"regions": ["上海"], "industries": [], "topics": ["用工政策"]}))
    source_run = NewsSourceRun(task_id=task.id, source_id=source.id, status="succeeded", config_snapshot=row(source))
    db.add(source_run)
    db.flush()
    ingest_news(db, task, source_run, [NewsDocument(
        "https://hr.example.invalid/policy/worker", "关于劳动保障实施办法的通知",
        "本办法自2032年1月2日起施行。" + "依法保障劳动者合法权益。" * 15,
        published_date=date(2032, 1, 1), content_extent="full_text")])
    db.commit()
    reminder = db.scalar(select(NewsPolicyReminder))
    assert reminder and reminder.status == "pending"
    requests = []
    def receive(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})
    monkeypatch.setattr(notifications, "destination_address", lambda _: "93.184.216.34")
    actual_outbox = notifications.process_outbox
    monkeypatch.setattr(notifications, "process_outbox", partial(actual_outbox, transport=httpx.MockTransport(receive)))
    assert workers.deliver_notifications.run() == 0
    at[0] = datetime(2032, 1, 2, 1, 0, tzinfo=timezone.utc)
    assert workers.deliver_notifications.run() == 1
    assert workers.deliver_notifications.run() == 0
    assert len(requests) == 1 and requests[0]["event_type"] == "news.policy_effective"
    assert requests[0]["policy"]["effective_date"] == "2032-01-02"
    db.expire_all()
    assert db.scalar(select(NotificationDelivery)).status == "sent"
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
