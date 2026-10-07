from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading

import httpx
import pytest
from sqlalchemy import func, select

from app.notification_digest import daily_window, queue_due_digests
from app.notification_models import (
    NotificationChannel, NotificationDelivery, NotificationDigestItem, NotificationEvent, NotificationRule,
)
from app.notifications import emit_event, encrypt_config, process_outbox


def moment(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


RECORDED = moment("2026-10-05T10:00:00+08:00")
DUE = moment("2026-10-06T09:00:00+08:00")
DISPATCH_DATES = [pytest.param(RECORDED, id="original-2026"),
                  pytest.param(moment("2020-02-28T10:00:00+08:00"), id="past-leap-day"),
                  pytest.param(moment("2035-12-31T10:00:00+08:00"), id="future-year-boundary")]


def subscription(db, **options):
    channel = NotificationChannel(name="摘要测试通道", kind="webhook", enabled=True,
                                  destination_hint="hooks.example.com", encrypted_config=encrypt_config({
                                      "url": "https://hooks.example.com/private-fixture", "secret": "fixture-secret"}))
    db.add(channel)
    db.flush()
    rule = NotificationRule(name="岗位日报", channel_id=channel.id,
                             event_types=["recruitment.first_seen", "news.new"], enabled=True,
                             delivery_mode="daily_digest", digest_time="09:00", timezone="Asia/Shanghai", **options)
    db.add(rule)
    db.commit()
    return channel, rule


def observe(db, monkeypatch, key, at=RECORDED, event_type="recruitment.first_seen", title="上海研发岗位", url=None):
    monkeypatch.setattr("app.notifications.now", lambda: at)
    return emit_event(db, event_type=event_type, dedupe_key=key, title=title,
                      text="岗位职责与来源原文", url=url or f"https://example.com/jobs/{key}")


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def due_delivery(db, monkeypatch, at):
    # SQLAlchemy's column default uses its own real clock. Align the fixture's
    # due time and sender clock explicitly, even when the wall clock passes RECORDED.
    delivery = db.scalar(select(NotificationDelivery))
    assert delivery is not None
    delivery.next_attempt_at = at
    db.commit()
    monkeypatch.setattr("app.notifications.now", lambda: at)
    return delivery


def test_shanghai_day_boundary_and_due_time_do_not_mix_days(db, monkeypatch):
    subscription(db)
    before = observe(db, monkeypatch, "before", moment("2026-10-05T23:59:59+08:00"))
    after = observe(db, monkeypatch, "after", moment("2026-10-06T00:00:00+08:00"))
    db.commit()
    assert count(db, NotificationDelivery) == 0
    assert queue_due_digests(at=moment("2026-10-06T08:59:59+08:00")) == 0
    assert queue_due_digests(at=DUE) == 1
    assert queue_due_digests(at=DUE) == 0
    digest = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "notification.digest"))
    data = digest.payload["digest"]
    assert data["window_start"] == "2026-10-04T16:00:00+00:00"
    assert data["window_end"] == "2026-10-05T16:00:00+00:00"
    assert [item["event_id"] for item in data["items"]] == [str(before)]
    assert data["items"][0]["included_at"] == "2026-10-05T15:59:59+00:00"
    assert "https://example.com/jobs/before" in digest.payload["text"]
    assert str(after) not in json.dumps(data)
    assert count(db, NotificationDigestItem) == 2


def test_rollback_and_repeated_business_event_never_duplicate_digest(db, monkeypatch):
    subscription(db)
    observe(db, monkeypatch, "rollback")
    db.rollback()
    assert count(db, NotificationDigestItem) == 0
    assert count(db, NotificationEvent) == 0
    first = observe(db, monkeypatch, "once")
    db.commit()
    assert observe(db, monkeypatch, "once") is None
    db.commit()
    assert count(db, NotificationDigestItem) == 1
    assert queue_due_digests(at=DUE) == 1
    digest = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "notification.digest"))
    assert digest.payload["digest"]["items"][0]["event_id"] == str(first)


def test_without_enabled_rule_no_capture_no_history_backfill(db, monkeypatch):
    old = observe(db, monkeypatch, "old")
    db.commit()
    assert queue_due_digests(at=DUE) == 0
    _, rule = subscription(db)
    rule.enabled = False
    db.commit()
    observe(db, monkeypatch, "disabled")
    db.commit()
    rule.enabled = True
    db.commit()
    assert queue_due_digests(at=DUE) == 0
    new = observe(db, monkeypatch, "new")
    db.commit()
    assert queue_due_digests(at=DUE) == 1
    digest = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "notification.digest"))
    ids = [item["event_id"] for item in digest.payload["digest"]["items"]]
    assert ids == [str(new)] and str(old) not in ids


def test_digest_respects_filters_but_does_not_drop_events_for_immediate_cooldown(db, monkeypatch):
    subscription(db, keywords=["上海"], cooldown_seconds=86400)
    observe(db, monkeypatch, "included-1")
    observe(db, monkeypatch, "excluded", title="北京岗位")
    observe(db, monkeypatch, "included-2", event_type="news.new")
    db.commit()
    assert count(db, NotificationDigestItem) == 2
    assert queue_due_digests(at=DUE) == 1
    digest = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "notification.digest"))
    assert {entry["event_type"] for entry in digest.payload["digest"]["items"]} == {"recruitment.first_seen", "news.new"}


def test_two_digest_dispatchers_create_one_durable_outbox(db, monkeypatch):
    subscription(db)
    for index in range(4):
        observe(db, monkeypatch, str(index))
    db.commit()
    barrier = threading.Barrier(2)
    def dispatch(_):
        barrier.wait(timeout=5)
        return queue_due_digests(at=DUE)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(dispatch, range(2)))
    assert sum(results) == 1
    assert count(db, NotificationDelivery) == 1
    assert count(db, NotificationEvent) == 5


def test_bounded_passes_preserve_every_event_and_report_ranges(db, monkeypatch):
    subscription(db)
    ids = [observe(db, monkeypatch, str(index)) for index in range(7)]
    db.commit()
    monkeypatch.setattr("app.notification_digest.MAX_ITEMS_PER_RULE_PASS", 3)
    assert [queue_due_digests(limit=1, at=DUE) for _ in range(4)] == [1, 1, 1, 0]
    digests = db.scalars(select(NotificationEvent).where(NotificationEvent.type == "notification.digest")
                         .order_by(NotificationEvent.created_at)).all()
    ranges = [(event.payload["digest"]["range_start"], event.payload["digest"]["range_end"]) for event in digests]
    assert ranges == [(1, 3), (4, 6), (7, 7)]
    sent_ids = [entry["event_id"] for event in digests for entry in event.payload["digest"]["items"]]
    assert len(sent_ids) == len(set(sent_ids)) == 7 and set(sent_ids) == {str(identifier) for identifier in ids}
    assert all(event.payload["digest"]["total_items"] == 7 for event in digests)


def test_long_digest_is_split_with_all_source_links(db, monkeypatch):
    subscription(db)
    urls = ["https://example.com/" + str(index) + "/" + "x" * 1750 for index in range(8)]
    for index, url in enumerate(urls):
        observe(db, monkeypatch, str(index), title="长标题" * 40, url=url)
    db.commit()
    assert queue_due_digests(at=DUE) > 1
    digests = db.scalars(select(NotificationEvent).where(NotificationEvent.type == "notification.digest")).all()
    assert {entry["url"] for event in digests for entry in event.payload["digest"]["items"]} == set(urls)
    assert all(len(event.payload["text"].encode()) < 9000 for event in digests)


@pytest.mark.parametrize("recorded_at", DISPATCH_DATES)
def test_channel_target_version_change_cancels_pending_and_queued_without_sending(db, monkeypatch, recorded_at):
    due = daily_window(recorded_at, "09:00")[2]
    channel, _ = subscription(db)
    observe(db, monkeypatch, "generated", at=recorded_at)
    db.commit()
    assert queue_due_digests(at=due) == 1
    observe(db, monkeypatch, "pending", at=recorded_at)
    db.commit()
    channel.encrypted_config = encrypt_config({"url": "https://other.example/new-target"})
    db.commit()
    assert queue_due_digests(at=due) == 0
    due_delivery(db, monkeypatch, due)
    def forbidden(request):
        pytest.fail("A changed recipient must not receive old digest material")
    assert process_outbox(transport=httpx.MockTransport(forbidden)) == 0
    db.expire_all()
    assert db.scalar(select(NotificationDelivery)).error_code == "channel_changed"
    assert db.scalar(select(NotificationDigestItem.status).where(NotificationDigestItem.digest_event_id.is_(None))) == "cancelled"


def test_rule_change_cancels_backlog_and_refreshes_long_lived_emitter(signed_client, db, monkeypatch):
    channel, rule = subscription(db)
    observe(db, monkeypatch, "old-mode")
    db.commit()
    response = signed_client.put(f"/api/v1/notification-rules/{rule.id}", json={
        "name": rule.name, "channel_id": str(channel.id), "event_types": rule.event_types,
        "enabled": True, "delivery_mode": "immediate"})
    assert response.status_code == 200 and response.json()["cancelled_pending_digest_items"] == 1
    # Deliberately reuse db's identity map after the other API transaction changed the rule.
    observe(db, monkeypatch, "new-mode")
    db.commit()
    assert queue_due_digests(at=DUE) == 0
    assert count(db, NotificationDelivery) == 1
    delivery = db.scalar(select(NotificationDelivery))
    assert db.get(NotificationEvent, delivery.event_id).type == "recruitment.first_seen"


def test_changing_digest_time_does_not_backfill_previous_window(signed_client, db, monkeypatch):
    channel, rule = subscription(db)
    observe(db, monkeypatch, "old-day")
    db.commit()
    response = signed_client.put(f"/api/v1/notification-rules/{rule.id}", json={
        "name": rule.name, "channel_id": str(channel.id), "event_types": rule.event_types,
        "enabled": True, "delivery_mode": "daily_digest", "digest_time": "08:00"})
    assert response.status_code == 200 and response.json()["cancelled_pending_digest_items"] == 1
    observe(db, monkeypatch, "new-day", moment("2026-10-06T10:00:00+08:00"))
    db.commit()
    assert queue_due_digests(at=DUE) == 0
    assert queue_due_digests(at=moment("2026-10-07T08:00:00+08:00")) == 1
    digest = db.scalar(select(NotificationEvent).where(NotificationEvent.type == "notification.digest"))
    assert digest.payload["digest"]["items"][0]["url"].endswith("/new-day")


def test_channel_edit_api_cancels_digest_before_new_target(signed_client, db, monkeypatch):
    channel, _ = subscription(db)
    observe(db, monkeypatch, "old-target")
    db.commit()
    assert queue_due_digests(at=DUE) == 1
    response = signed_client.put(f"/api/v1/notification-channels/{channel.id}", json={
        "name": "新目标", "kind": "webhook", "url": "https://other.example/new-target", "enabled": True})
    assert response.status_code == 200
    db.expire_all()
    delivery = db.scalar(select(NotificationDelivery))
    assert delivery.status == "cancelled" and delivery.error_code == "channel_changed"
    assert signed_client.post(f"/api/v1/notification-deliveries/{delivery.id}/retry").status_code == 409


def test_channel_edit_does_not_cancel_same_event_for_another_channel(signed_client, db, monkeypatch):
    channel, rule = subscription(db)
    rule.delivery_mode = "immediate"
    other_channel, other_rule = subscription(db)
    other_rule.delivery_mode = "immediate"
    db.commit()
    observe(db, monkeypatch, "two-recipients")
    db.commit()
    assert count(db, NotificationDelivery) == 2
    response = signed_client.put(f"/api/v1/notification-channels/{channel.id}", json={
        "name": "新目标", "kind": "webhook", "url": "https://other.example/new-target", "enabled": True})
    assert response.status_code == 200
    db.expire_all()
    states = {delivery.channel_id: delivery.status for delivery in db.scalars(select(NotificationDelivery))}
    assert states[channel.id] == "cancelled" and states[other_channel.id] == "queued"


@pytest.mark.parametrize("recorded_at", DISPATCH_DATES)
def test_digest_retry_keeps_event_identity_and_read_timeout_stays_unknown(db, monkeypatch, recorded_at):
    due = daily_window(recorded_at, "09:00")[2]
    subscription(db)
    observe(db, monkeypatch, "retry", at=recorded_at)
    db.commit()
    assert queue_due_digests(at=due) == 1
    due_delivery(db, monkeypatch, due)
    monkeypatch.setattr("app.notifications.destination_address", lambda _: "93.184.216.34")
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429)
        raise httpx.ReadTimeout("ambiguous", request=request)
    transport = httpx.MockTransport(handler)
    process_outbox(transport=transport)
    db.expire_all()
    delivery = db.scalar(select(NotificationDelivery))
    assert delivery.status == "retrying"
    retry_at = delivery.next_attempt_at
    assert retry_at == due + timedelta(seconds=60)
    monkeypatch.setattr("app.notifications.now", lambda: retry_at - timedelta(microseconds=1))
    assert process_outbox(transport=transport) == 0 and len(requests) == 1
    monkeypatch.setattr("app.notifications.now", lambda: retry_at)
    process_outbox(transport=transport)
    db.expire_all()
    assert delivery.status == "unknown"
    assert process_outbox(transport=transport) == 0
    assert len(requests) == 2
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]
    assert json.loads(requests[0].content)["schema_version"] == 1
    assert json.loads(requests[0].content)["digest"]["items"][0]["url"] == "https://example.com/jobs/retry"


def test_digest_api_selection_pending_window_and_detail_links(signed_client, db, monkeypatch):
    channel, _ = subscription(db)
    response = signed_client.post("/api/v1/notification-rules", json={
        "name": "资讯日报", "channel_id": str(channel.id), "event_types": ["news.new"],
        "delivery_mode": "daily_digest", "digest_time": "09:30", "timezone": "Asia/Shanghai", "enabled": True})
    assert response.status_code == 201
    rule_id = response.json()["id"]
    observe(db, monkeypatch, "news", event_type="news.new")
    db.commit()
    groups = signed_client.get("/api/v1/notification-digest-pending").json()
    group = next(item for item in groups if item["rule_id"] == rule_id)
    assert group["event_count"] == 1 and moment(group["scheduled_for"]) == moment("2026-10-06T09:30:00+08:00")
    queue_due_digests(at=moment("2026-10-06T10:00:00+08:00"))
    deliveries = signed_client.get("/api/v1/notification-deliveries").json()
    selected = next(item for item in deliveries if item["digest"]["rule_id"] == rule_id)
    assert "items" not in selected["digest"] and "channel_updated_at" not in selected["digest"]
    detail = signed_client.get("/api/v1/notification-deliveries/" + selected["id"]).json()
    assert detail["digest"]["items"][0]["url"] == "https://example.com/jobs/news"
    assert "private-fixture" not in json.dumps(detail) and "fixture-secret" not in json.dumps(detail)


@pytest.mark.parametrize("field, value", [("digest_time", "24:00"), ("digest_time", "9:00"), ("timezone", "America/New_York"), ("delivery_mode", "surprise")])
def test_digest_schedule_rejects_ambiguous_inputs(signed_client, db, field, value):
    channel, _ = subscription(db)
    payload = {"name": "无效", "channel_id": str(channel.id), "event_types": ["news.new"],
               "delivery_mode": "daily_digest", "digest_time": "09:00", "timezone": "Asia/Shanghai", field: value}
    assert signed_client.post("/api/v1/notification-rules", json=payload).status_code == 422


def test_naive_digest_clock_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        daily_window(datetime(2026, 10, 5), "09:00")
