"""Calendar-day digests, captured with business events and dispatched through the outbox."""
import hashlib
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urlparse
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.config import get_settings
from app.db import session_factory
from app.models import now
from app.notification_models import (
    NotificationChannel, NotificationDelivery, NotificationDigestItem, NotificationEvent, NotificationRule,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
MAX_PART_BYTES = 6000
MAX_ITEMS_PER_PART = 10
MAX_ITEMS_PER_RULE_PASS = 500


def utc_stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def daily_window(recorded_at: datetime, digest_time: str):
    if recorded_at.tzinfo is None:
        raise ValueError("Digest timestamps require a timezone")
    local = recorded_at.astimezone(SHANGHAI)
    start = datetime.combine(local.date(), time.min, SHANGHAI)
    end = start + timedelta(days=1)
    hour, minute = map(int, digest_time.split(":"))
    scheduled = end + timedelta(hours=hour, minutes=minute)
    return start, end, scheduled


def capture_digest_item(db, rule, channel, event_id, *, recorded_at=None):
    recorded_at = recorded_at or now()
    start, end, scheduled = daily_window(recorded_at, rule.digest_time)
    db.execute(insert(NotificationDigestItem).values(
        id=uuid.uuid4(), rule_id=rule.id, channel_id=channel.id,
        channel_updated_at=channel.updated_at, source_event_id=event_id,
        window_start=start, window_end=end, scheduled_for=scheduled,
        status="pending", created_at=recorded_at, updated_at=recorded_at,
    ).on_conflict_do_nothing(index_elements=["rule_id", "source_event_id"]))


def cancel_pending(db, *, rule_id=None, channel_id=None, reason="rule_changed"):
    from app.notifications import lock_notification_state
    lock_notification_state(db)
    item_query = update(NotificationDigestItem).where(NotificationDigestItem.status == "pending")
    events = select(NotificationEvent.id).where(NotificationEvent.type == "notification.digest")
    if rule_id is not None:
        item_query = item_query.where(NotificationDigestItem.rule_id == rule_id)
        events = events.where(NotificationEvent.payload["digest"]["rule_id"].astext == str(rule_id))
    elif channel_id is not None:
        item_query = item_query.where(NotificationDigestItem.channel_id == channel_id)
        events = select(NotificationDelivery.event_id).where(NotificationDelivery.channel_id == channel_id)
    else:
        raise ValueError("A rule or channel scope is required")
    count = db.execute(item_query.values(status="cancelled")).rowcount
    delivery_query = update(NotificationDelivery).where(
        NotificationDelivery.event_id.in_(events), NotificationDelivery.status.in_(["queued", "retrying"]),
    )
    if channel_id is not None:
        delivery_query = delivery_query.where(NotificationDelivery.channel_id == channel_id)
    db.execute(delivery_query.values(status="cancelled", error_code=reason))
    return count


def source_link(value):
    value = str(value or "")
    try:
        parsed = urlparse(value)
        if parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password:
            return value[:2000]
    except ValueError:
        pass
    return ""


def _entry(event, included_at):
    payload = event.payload
    return {"event_id": str(event.id), "event_type": event.type,
            "title": str(payload.get("title", ""))[:160],
            "summary": str(payload.get("text", ""))[:180],
            "url": source_link(payload.get("url")), "observed_at": utc_stamp(event.created_at),
            "included_at": utc_stamp(included_at)}


def _line(entry):
    return f"• {entry['title']}\n{entry['summary']}\n{entry['url']}\n"


def _parts(rows):
    part, length = [], 0
    for item, event in rows:
        entry = _entry(event, item.created_at)
        size = len(_line(entry).encode())
        if part and (len(part) >= MAX_ITEMS_PER_PART or length + size > MAX_PART_BYTES):
            yield part
            part, length = [], 0
        part.append((item, entry))
        length += size
    if part:
        yield part


def queue_due_digests(limit=20, at=None):
    """Return generated outbox count. Bounded work, row locks and one DB transaction per rule."""
    at = at or now()
    if at.tzinfo is None:
        raise ValueError("Digest timestamps require a timezone")
    generated = 0
    for _ in range(max(0, min(limit, 100))):
        with session_factory()() as db:
            from app.notifications import lock_notification_state
            lock_notification_state(db)
            due = exists(select(NotificationDigestItem.id).where(
                NotificationDigestItem.rule_id == NotificationRule.id,
                NotificationDigestItem.status == "pending", NotificationDigestItem.scheduled_for <= at))
            rule = db.scalar(select(NotificationRule).where(due).order_by(NotificationRule.id)
                             .with_for_update(skip_locked=True).limit(1))
            if rule is None:
                break
            channel = db.scalar(select(NotificationChannel).where(NotificationChannel.id == rule.channel_id)
                                .with_for_update())
            if not rule.enabled or rule.delivery_mode != "daily_digest" or not channel or not channel.enabled:
                cancel_pending(db, rule_id=rule.id, reason="digest_rule_disabled")
                db.commit()
                continue
            # Do not redirect old subscriptions after a channel URL/recipient change.
            db.execute(update(NotificationDigestItem).where(
                NotificationDigestItem.rule_id == rule.id, NotificationDigestItem.status == "pending",
                (NotificationDigestItem.channel_id != channel.id)
                | (NotificationDigestItem.channel_updated_at != channel.updated_at),
            ).values(status="cancelled"))
            first = db.scalar(select(NotificationDigestItem).where(
                NotificationDigestItem.rule_id == rule.id, NotificationDigestItem.status == "pending",
                NotificationDigestItem.scheduled_for <= at).order_by(NotificationDigestItem.window_start).limit(1))
            if first is None:
                db.commit()
                continue
            rows = db.execute(select(NotificationDigestItem, NotificationEvent).join(
                NotificationEvent, NotificationDigestItem.source_event_id == NotificationEvent.id,
            ).where(NotificationDigestItem.rule_id == rule.id, NotificationDigestItem.status == "pending",
                    NotificationDigestItem.window_start == first.window_start,
                    NotificationDigestItem.scheduled_for == first.scheduled_for)
                .order_by(NotificationDigestItem.created_at, NotificationDigestItem.id)
                .limit(MAX_ITEMS_PER_RULE_PASS)).all()
            same_window = (NotificationDigestItem.rule_id == rule.id,
                           NotificationDigestItem.window_start == first.window_start,
                           NotificationDigestItem.scheduled_for == first.scheduled_for)
            total = db.scalar(select(func.count()).select_from(NotificationDigestItem).where(
                *same_window, NotificationDigestItem.status != "cancelled"))
            offset = db.scalar(select(func.count()).select_from(NotificationDigestItem).where(
                *same_window, NotificationDigestItem.status == "batched"))
            for part in _parts(rows):
                entries = [entry for _, entry in part]
                stable_ids = sorted(str(item.id) for item, _ in part)
                digest_key = hashlib.sha256("|".join(stable_ids).encode()).hexdigest()
                day = first.window_start.astimezone(SHANGHAI).date().isoformat()
                start_index, end_index = offset + 1, offset + len(entries)
                title = f"{rule.name} · {day} 日报（{start_index}–{end_index}/{total}条）"[:250]
                text = (f"北京时间 {day} 00:00 至 {first.window_end.astimezone(SHANGHAI):%Y-%m-%d} 00:00\n"
                        f"本摘要第 {start_index}–{end_index} 条，共 {total} 条；较多事件分片发送。\n"
                        "按平台接收事件日期汇总；不是原文发布日期。\n\n"
                        + "\n".join(_line(entry) for entry in entries))
                event_id = db.execute(insert(NotificationEvent).values(
                    id=uuid.uuid4(), type="notification.digest", dedupe_key=f"digest:{rule.id}:{digest_key}",
                    payload={"title": title, "text": text, "url": get_settings().app_url.rstrip("/") + "/notifications",
                             "digest": {"rule_id": str(rule.id), "rule_name": rule.name,
                                        "window_start": utc_stamp(first.window_start), "window_end": utc_stamp(first.window_end),
                                        "timezone": "Asia/Shanghai", "scheduled_for": utc_stamp(first.scheduled_for),
                                        "channel_updated_at": utc_stamp(channel.updated_at),
                                        "item_count": len(entries), "items": entries,
                                        "range_start": start_index, "range_end": end_index, "total_items": total,
                                        "part_notice": "为控制消息长度，多条摘要共同覆盖本日事件。"}},
                ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(NotificationEvent.id)).scalar()
                if event_id is None:
                    event_id = db.scalar(select(NotificationEvent.id).where(
                        NotificationEvent.dedupe_key == f"digest:{rule.id}:{digest_key}"))
                delivery_id = db.execute(insert(NotificationDelivery).values(
                    id=uuid.uuid4(), event_id=event_id, channel_id=channel.id,
                ).on_conflict_do_nothing(index_elements=["event_id", "channel_id"])
                    .returning(NotificationDelivery.id)).scalar()
                for item, _ in part:
                    item.status, item.digest_event_id = "batched", event_id
                generated += bool(delivery_id)
                offset = end_index
            db.commit()
    return generated


def pending_groups(db):
    return [dict(row._mapping) for row in db.execute(select(
        NotificationDigestItem.rule_id, NotificationRule.name.label("rule_name"),
        NotificationChannel.name.label("channel_name"), NotificationDigestItem.window_start,
        NotificationDigestItem.window_end, NotificationDigestItem.scheduled_for,
        func.count(NotificationDigestItem.id).label("event_count"),
    ).join(NotificationRule, NotificationDigestItem.rule_id == NotificationRule.id)
        .join(NotificationChannel, NotificationDigestItem.channel_id == NotificationChannel.id)
        .where(NotificationDigestItem.status == "pending")
        .group_by(NotificationDigestItem.rule_id, NotificationRule.name, NotificationChannel.name,
                  NotificationDigestItem.window_start, NotificationDigestItem.window_end, NotificationDigestItem.scheduled_for)
        .order_by(NotificationDigestItem.scheduled_for).limit(100))]
