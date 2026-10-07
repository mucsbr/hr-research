"""News subscriptions and policy lifecycle on real PG, only Mock receivers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
import json
from threading import Event

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db import get_db, session_factory
from app.models import ModelCall, Subject, TaskRun, User
from app.news_models import NewsItem, NewsSource, NewsSourceRun
from app.news_policy_reminders import (
    policy_due, queue_due_policy_reminders, sync_policy_reminder,
)
from app.news_service import NewsDocument, ingest_news
from app.notification_api import router
from app.notification_digest import queue_due_digests
from app.notification_models import (
    NewsPolicyReminder, NotificationChannel, NotificationDelivery, NotificationDigestItem,
    NotificationEvent, NotificationRule,
)
from app.notifications import emit_event, encrypt_config, lock_notification_state, process_outbox
from app.security import current_user
from app.serialization import row


def moment(value):
    return datetime.fromisoformat(value)


@pytest.fixture
def clock(engine, monkeypatch):
    value = [moment("2026-10-20T10:00:00+08:00")]
    for module in ["app.news_policy_reminders", "app.news_service", "app.notifications"]:
        monkeypatch.setattr(module + ".now", lambda: value[0])
    monkeypatch.setattr("app.notifications.destination_address", lambda _: "93.184.216.34")
    return value


@pytest.fixture
def api(db):
    user = User(username="policy-admin", role="admin", password_hash="unused")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client


def subscription(db, *, channel=None, event_types=None, **fields):
    if channel is None:
        channel = NotificationChannel(name="隔离接收器", kind="webhook", enabled=True,
            destination_hint="hooks.example.invalid", encrypted_config=encrypt_config({"url": "https://hooks.example.invalid/receiver"}))
        db.add(channel)
        db.flush()
    rule = NotificationRule(name="资讯订阅", channel_id=channel.id, enabled=True,
        event_types=event_types or ["news.policy_effective"], **fields)
    db.add(rule)
    db.commit()
    return channel, rule


def observe(db, *, source=None, effective="2026年11月1日", body=None, extent="full_text", source_type="official",
            url="https://hr.example.invalid/policy/1"):
    if source is None:
        subject = Subject(type="industry", name="QA用工主题")
        db.add(subject)
        db.flush()
        source = NewsSource(name="QA官方来源", kind="web", url="https://hr.example.invalid/", source_type=source_type,
            subject_id=subject.id, regions=["上海"], industries=["制造业"], topics=["用工政策"])
        db.add(source)
        db.flush()
    task = TaskRun(domain="news", kind="news_collection")
    db.add(task)
    db.flush()
    run = NewsSourceRun(task_id=task.id, source_id=source.id, config_snapshot=row(source))
    db.add(run)
    db.flush()
    document = NewsDocument(url, "关于劳动保障实施办法的通知",
        body or f"本办法自{effective}起施行。" + "依法保障劳动者合法权益。" * 15,
        published_date=date(2026, 10, 20), content_extent=extent)
    ingest_news(db, task, run, [document])
    db.flush()
    return db.scalar(select(NewsItem)), source, run


def emit_news(db, key, *, tags=None, subject_id=None):
    return emit_event(db, event_type="news.new", dedupe_key=key, title="劳动保障资讯", text="原文摘要",
        subject_id=subject_id, news=tags or {}, url="https://hr.example.invalid/news/1")


def test_rule_schema_defaults_normalization_and_admin_gate(api, db, clock):
    channel, _ = subscription(db)
    base = {"name": "标签规则", "channel_id": str(channel.id), "event_types": ["news.new"]}
    saved = api.post("/api/v1/notification-rules", json={**base,
        "news_filters": {"regions": [" 上海 ", "上海"], "industries": ["IT", "it"], "topics": []}})
    assert saved.status_code == 201 and saved.json()["enabled"] is False
    assert saved.json()["news_filters"] == {"regions": ["上海"], "industries": ["it"], "topics": []}
    legacy = api.post("/api/v1/notification-rules", json=base)
    assert legacy.json()["news_filters"] == {"regions": [], "industries": [], "topics": []}
    for fields in [{"event_types": ["news.policy_effective"], "delivery_mode": "daily_digest"},
        {"event_types": ["news.new", "recruitment.first_seen"], "news_filters": {"regions": ["上海"]}},
        {"news_filters": {"regions": [" "]}}, {"news_filters": {"topics": ["x" * 151]}},
        {"news_filters": {"industries": [str(i) for i in range(31)]}}]:
        assert api.post("/api/v1/notification-rules", json={**base, **fields}).status_code == 422
    user = db.scalar(select(User).where(User.username == "policy-admin"))
    user.role = "user"
    db.commit()
    assert api.post("/api/v1/notification-rules", json=base).status_code == 403


def test_structured_or_and_matching_is_identical_for_immediate_and_digest(db, clock):
    subject = Subject(name="目标主题")
    db.add(subject)
    db.commit()
    filters = {"regions": ["上海", "北京"], "industries": ["manufacturing"], "topics": ["社保"]}
    subscription(db, event_types=["news.new"], news_filters=filters, subject_ids=[str(subject.id)], keywords=["劳动"])
    subscription(db, event_types=["news.new"], news_filters=filters, subject_ids=[str(subject.id)], keywords=["劳动"],
                 delivery_mode="daily_digest")
    good = {"regions": [" 北京 "], "industries": ["Manufacturing"], "topics": ["社保", "其他"]}
    emit_news(db, "match", tags=good, subject_id=subject.id)
    emit_news(db, "missing", tags={**good, "industries": []}, subject_id=subject.id)
    emit_news(db, "wrong-region", tags={**good, "regions": ["全国"]}, subject_id=subject.id)
    emit_news(db, "wrong-subject", tags=good)
    db.commit()
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
    assert db.scalar(select(func.count()).select_from(NotificationDigestItem)) == 1
    assert queue_due_digests(at=clock[0] + timedelta(days=1)) == 1
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 2


def test_real_news_ingest_attaches_tags_and_context_has_only_ids_and_hash(db, clock):
    subscription(db, event_types=["news.policy"], news_filters={"regions": ["上海"]})
    item, source, run = observe(db)
    db.commit()
    event = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "news.policy"))
    assert event.payload["news"]["industries"] == ["制造业"] and event.payload["news"]["item_id"] == str(item.id)
    reminder = db.scalar(select(NewsPolicyReminder))
    assert set(reminder.context) == {"subject_id", "source_id", "source_run_id", "basis_sha256"}
    assert reminder.context["source_run_id"] == str(run.id) and reminder.context["source_id"] == str(source.id)
    assert len(reminder.context["basis_sha256"]) == 64
    assert "http" not in json.dumps(reminder.context) and "联系人" not in json.dumps(reminder.context)


@pytest.mark.parametrize("source_type,extent,body", [("media", "full_text", None), ("official", "feed_summary", None),
    ("official", "full_text", "适用时间尚未确定。" * 12),
    ("official", "full_text", "本办法自2026年11月1日起施行；另一部分自2026年12月1日起施行。")])
def test_only_official_full_text_with_one_date_is_registered(db, clock, source_type, extent, body):
    observe(db, source_type=source_type, extent=extent, body=body)
    db.commit()
    assert db.scalar(select(NewsPolicyReminder)) is None


@pytest.mark.parametrize("seen,due,expected", [
    ("2026-11-02T00:00:00+08:00", "2026-11-02T09:00:00+08:00", 0),
    ("2026-11-01T08:00:00+08:00", "2026-11-01T08:59:59+08:00", 0),
    ("2026-11-01T08:00:00+08:00", "2026-11-01T09:00:00+08:00", 1),
    ("2026-11-01T11:00:00+08:00", "2026-11-01T11:00:00+08:00", 1)])
def test_first_seen_past_and_shanghai_nine_oclock_boundaries(db, clock, seen, due, expected):
    clock[0] = moment(seen)
    subscription(db)
    observe(db)
    db.commit()
    before_tasks = db.scalar(select(func.count()).select_from(TaskRun))
    assert queue_due_policy_reminders(at=moment(due)) == expected
    assert queue_due_policy_reminders(at=moment(due)) == 0
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == expected
    assert db.scalar(select(func.count()).select_from(TaskRun)) == before_tasks
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_delayed_existing_reminder_and_two_dispatchers_emit_once(db, clock):
    subscription(db)
    observe(db)
    db.commit()
    due = moment("2026-11-02T10:00:00+08:00")
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: queue_due_policy_reminders(at=due), range(2)))
    assert sum(counts) == 1
    db.expire_all()
    event = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "news.policy_effective"))
    assert event.payload["policy"]["timing"] == "延迟提醒"
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1


def test_policy_sync_and_event_outbox_rollback_are_atomic(db, clock):
    subscription(db)
    observe(db)
    db.rollback()
    assert db.scalar(select(NewsPolicyReminder)) is None and db.scalar(select(NewsItem)) is None
    assert db.scalar(select(NotificationEvent)) is None and db.scalar(select(NotificationDelivery)) is None


def test_policy_dispatch_is_bounded_and_rejects_naive_clock(db, clock):
    _, source, _ = observe(db)
    observe(db, source=source, url="https://hr.example.invalid/policy/2",
            body="本办法自2026年11月1日起施行。另一条政策关注社会保险。" * 10)
    db.commit()
    with pytest.raises(ValueError, match="timezone"):
        queue_due_policy_reminders(at=datetime(2026, 11, 1, 9))
    due = policy_due(date(2026, 11, 1))
    assert [queue_due_policy_reminders(limit=1, at=due) for _ in range(3)] == [1, 1, 0]
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(NewsPolicyReminder)) == 2
    assert not db.scalars(select(NotificationDelivery)).all()


def test_policy_processed_without_enabled_rule_is_not_backfilled_after_enable(db, clock, api):
    _, rule = subscription(db)
    rule.enabled = False
    db.commit()
    observe(db)
    db.commit()
    clock[0] = policy_due(date(2026, 11, 1))
    assert queue_due_policy_reminders(at=clock[0]) == 1
    assert not db.scalars(select(NotificationDelivery)).all()
    assert api.patch(f"/api/v1/notification-rules/{rule.id}", json={"enabled": True}).status_code == 200
    assert queue_due_policy_reminders(at=clock[0]) == 0
    assert not db.scalars(select(NotificationDelivery)).all()


@pytest.mark.parametrize("mode", ["immediate", "daily_digest"])
def test_editing_structured_filter_cancels_only_unmatched_pending_work(db, clock, api, mode):
    channel, rule = subscription(db, event_types=["news.new"], delivery_mode=mode, news_filters={"regions": ["上海"]})
    emit_news(db, "before-edit", tags={"regions": ["上海"]})
    db.commit()
    result = api.put(f"/api/v1/notification-rules/{rule.id}", json={"name": rule.name,
        "channel_id": str(channel.id), "event_types": ["news.new"], "enabled": True, "delivery_mode": mode,
        "news_filters": {"regions": ["北京"]}})
    assert result.status_code == 200
    db.expire_all()
    if mode == "immediate":
        assert result.json()["cancelled_pending_news_deliveries"] == 1
        assert db.scalar(select(NotificationDelivery)).status == "cancelled"
    else:
        assert result.json()["cancelled_pending_digest_items"] == 1
        assert db.scalar(select(NotificationDigestItem)).status == "cancelled"
        assert queue_due_digests(at=clock[0] + timedelta(days=1)) == 0


def test_future_date_a_b_a_can_restore_only_unprocessed_plan(db, clock):
    _, source, _ = observe(db)
    db.commit()
    observe(db, source=source, effective="2026年12月1日")
    db.commit()
    observe(db, source=source)
    db.commit()
    rows = db.scalars(select(NewsPolicyReminder).order_by(NewsPolicyReminder.effective_date)).all()
    assert [r.status for r in rows] == ["pending", "cancelled"] and all(r.event_id is None for r in rows)


@pytest.mark.parametrize("outcome", ["queued", "sent", "unknown"])
def test_processed_date_does_not_revive_after_date_a_b_a(db, clock, api, outcome):
    subscription(db)
    _, source, _ = observe(db)
    db.commit()
    clock[0] = policy_due(date(2026, 11, 1))
    assert queue_due_policy_reminders(at=clock[0]) == 1
    received = []
    def receiver(request):
        received.append(request)
        if outcome == "unknown":
            raise httpx.ReadTimeout("fixture ambiguity", request=request)
        return httpx.Response(204)
    if outcome != "queued":
        process_outbox(transport=httpx.MockTransport(receiver))
    db.expire_all()
    delivery = db.scalar(select(NotificationDelivery))
    old_id = delivery.id
    observe(db, source=source, effective="2026年12月1日")
    db.commit()
    observe(db, source=source)
    db.commit()
    assert queue_due_policy_reminders(at=clock[0]) == 0
    db.expire_all()
    assert db.get(NotificationDelivery, old_id).status == ("cancelled" if outcome == "queued" else outcome)
    assert api.post(f"/api/v1/notification-deliveries/{old_id}/retry", json={"acknowledge_duplicate": True}).status_code == 409
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1


def test_disabled_rules_do_not_backfill_and_one_shared_channel_rule_can_survive(db, clock, api):
    channel, first = subscription(db, event_types=["news.new"], news_filters={"regions": ["上海"]})
    _, second = subscription(db, channel=channel, event_types=["news.new"], news_filters={"regions": ["上海"]})
    emit_news(db, "shared", tags={"regions": ["上海"]})
    db.commit()
    assert api.patch(f"/api/v1/notification-rules/{first.id}", json={"enabled": False}).status_code == 200
    db.expire_all()
    assert db.scalar(select(NotificationDelivery)).status == "queued"
    assert api.patch(f"/api/v1/notification-rules/{second.id}", json={"enabled": False}).status_code == 200
    db.expire_all()
    assert db.scalar(select(NotificationDelivery)).status == "cancelled"
    assert api.patch(f"/api/v1/notification-rules/{first.id}", json={"enabled": True}).status_code == 200
    assert process_outbox(transport=httpx.MockTransport(lambda _: pytest.fail("no history backfill"))) == 0


def test_policy_edit_before_claim_prevents_send_but_no_lock_is_held_during_network(db, clock):
    subscription(db)
    item, source, run = observe(db)
    db.commit()
    clock[0] = policy_due(date(2026, 11, 1))
    queue_due_policy_reminders(at=clock[0])
    item_id, run_id = item.id, run.id
    def receiver(_request):
        # This must finish while send_webhook is still running, proving the gate
        # was committed before transport. In-flight notifications cannot be recalled.
        with session_factory()() as other:
            other.execute(text("SET LOCAL lock_timeout = '1s'"))
            lock_notification_state(other)
            live = other.get(NewsItem, item_id)
            live.effective_date = date(2026, 12, 1)
            live.classification = {**live.classification, "date_candidates": {"effective_date_candidates": ["2026-12-01"]}}
            sync_policy_reminder(other, live, other.get(NewsSourceRun, run_id), at=clock[0])
            other.commit()
        return httpx.Response(204)
    assert process_outbox(transport=httpx.MockTransport(receiver)) == 1
    db.expire_all()
    assert db.scalar(select(NotificationDelivery)).status == "sent"
    # The newly registered December event is cancelled before a sender can claim it.
    clock[0] = policy_due(date(2026, 12, 1))
    queue_due_policy_reminders(at=clock[0])
    observe(db, source=source, body="施行日期尚未确定。" * 10)
    db.commit()
    assert process_outbox(transport=httpx.MockTransport(lambda _: pytest.fail("invalidated policy was sent"))) == 0


def test_task_lock_then_emitter_and_gate_then_policy_never_reverse_task_lock(db, clock, monkeypatch):
    item, _, run = observe(db)
    db.commit()
    task_id = run.task_id
    task_locked, gate_held, emitter_started = Event(), Event(), Event()
    import app.news_policy_reminders as policies
    original_gate = policies.lock_notification_state
    def synchronized_gate(session):
        original_gate(session)
        gate_held.set()
        assert emitter_started.wait(2)
    def task_emitter():
        with session_factory()() as session:
            session.execute(text("SET LOCAL lock_timeout = '2s'"))
            session.scalar(select(TaskRun).where(TaskRun.id == task_id).with_for_update())
            task_locked.set()
            assert gate_held.wait(2)
            emitter_started.set()
            emit_event(session, event_type="sentiment.new", dedupe_key="lock-order", title="QA", text="QA", task_id=task_id)
            session.commit()
    def policy_dispatch():
        assert task_locked.wait(2)
        return queue_due_policy_reminders(limit=1, at=policy_due(date(2026, 11, 1)))
    monkeypatch.setattr(policies, "lock_notification_state", synchronized_gate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(task_emitter), pool.submit(policy_dispatch)]
        for future in futures:
            future.result(timeout=5)
    db.expire_all()
    assert db.scalar(select(NotificationEvent).where(NotificationEvent.dedupe_key == "lock-order"))
    policy_event = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "news.policy_effective"))
    assert policy_event.task_id is None
    assert not get_settings().smtp_host
