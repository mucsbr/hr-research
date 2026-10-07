"""Dated policy events on the existing outbox; no network or model calls."""
from datetime import datetime, time, timezone
import hashlib
import json
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.db import session_factory
from app.models import now
from app.news_models import NewsItem
from app.notification_models import NewsPolicyReminder, NotificationDelivery, NotificationEvent
from app.notifications import NEWS_FILTER_KEYS, emit_event, lock_notification_state, normalized_tags
from app.repair_evidence import clean_url, redact_text

SHANGHAI = ZoneInfo("Asia/Shanghai")


def news_metadata(item):
    return {"item_id": str(item.id), "source_type": item.source_type, "content_type": item.content_type,
            "content_extent": (item.classification or {}).get("content_extent", "unknown"),
            "effective_date": item.effective_date.isoformat() if item.effective_date else None,
            **{key: normalized_tags(getattr(item, key)) for key in NEWS_FILTER_KEYS}}


def eligible_policy(item):
    return bool(item and item.source_type == "official" and item.content_type == "policy_document"
        and item.effective_date and (item.classification or {}).get("content_extent") == "full_text"
        and (item.classification or {}).get("date_candidates", {}).get("effective_date_candidates")
            == [item.effective_date.isoformat()])


def policy_basis(item, context):
    # Persist a digest, not source credentials, article contacts or free-form text.
    metadata = {**news_metadata(item), "url_sha256": hashlib.sha256(item.url.encode()).hexdigest(),
                "subject_id": context.get("subject_id"), "source_id": context.get("source_id")}
    return hashlib.sha256(json.dumps(metadata, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def policy_due(effective_date):
    return datetime.combine(effective_date, time(9), SHANGHAI).astimezone(timezone.utc)


def _stamp(at):
    at = at or now()
    if at.tzinfo is None:
        raise ValueError("Policy reminder timestamps require a timezone")
    return at


def _cancel(db, reminder, reason):
    reminder.status, reminder.error_code = "cancelled", reason
    if reminder.event_id:
        db.execute(update(NotificationDelivery).where(NotificationDelivery.event_id == reminder.event_id,
            NotificationDelivery.status.in_(["queued", "retrying"])).values(status="cancelled", error_code=reason))


def sync_policy_reminder(db, item, source_run, *, at=None):
    """Call in the primary-news update transaction, holding notification gate first."""
    lock_notification_state(db)
    at = _stamp(at)
    context = {"subject_id": source_run.config_snapshot.get("subject_id"),
               "source_id": str(source_run.source_id), "source_run_id": str(source_run.id)}
    context["basis_sha256"] = policy_basis(item, context)
    rows = db.scalars(select(NewsPolicyReminder).where(NewsPolicyReminder.item_id == item.id)
                      .with_for_update().execution_options(populate_existing=True)).all()
    valid = eligible_policy(item)
    current = None
    for reminder in rows:
        if not valid or reminder.effective_date != item.effective_date:
            _cancel(db, reminder, "policy_changed")
        elif reminder.event_id:
            # Once materialized, a date is never automatically reissued, including A→B→A.
            if reminder.context.get("basis_sha256") != context["basis_sha256"]:
                _cancel(db, reminder, "policy_changed")
            current = reminder
        else:
            current = reminder
            if reminder.status == "pending" or item.effective_date >= at.astimezone(SHANGHAI).date():
                reminder.context = context
                reminder.status, reminder.error_code = "pending", None
    if valid and current is None and item.effective_date >= at.astimezone(SHANGHAI).date():
        current = NewsPolicyReminder(item_id=item.id, effective_date=item.effective_date,
            scheduled_for=policy_due(item.effective_date), status="pending", context=context)
        db.add(current)
    return current


def policy_event_error(db, event):
    try:
        identifier = uuid.UUID(event.payload.get("policy", {}).get("reminder_id", ""))
    except (ValueError, TypeError, AttributeError):
        return "policy_evidence_missing"
    reminder = db.get(NewsPolicyReminder, identifier, populate_existing=True)
    if not reminder or reminder.event_id != event.id or reminder.status != "emitted":
        return "policy_changed"
    item = db.get(NewsItem, reminder.item_id, populate_existing=True)
    if (not eligible_policy(item) or item.effective_date != reminder.effective_date
            or policy_basis(item, reminder.context) != reminder.context.get("basis_sha256")
            or event.payload["policy"].get("basis_sha256") != reminder.context.get("basis_sha256")):
        return "policy_changed"
    return None


def queue_due_policy_reminders(limit=20, at=None):
    """Bounded one-row transactions; returns new durable event count, not sent count."""
    at = _stamp(at)
    created = 0
    for _ in range(max(0, min(limit, 100))):
        with session_factory()() as db:
            lock_notification_state(db)
            reminder = db.scalar(select(NewsPolicyReminder).where(
                NewsPolicyReminder.status == "pending", NewsPolicyReminder.scheduled_for <= at)
                .order_by(NewsPolicyReminder.scheduled_for, NewsPolicyReminder.id)
                .with_for_update(skip_locked=True).limit(1))
            if reminder is None:
                break
            item = db.get(NewsItem, reminder.item_id)
            if (not eligible_policy(item) or item.effective_date != reminder.effective_date
                    or policy_basis(item, reminder.context) != reminder.context.get("basis_sha256")):
                _cancel(db, reminder, "policy_changed")
                db.commit()
                continue
            timing = "今日施行" if at.astimezone(SHANGHAI).date() == reminder.effective_date else "延迟提醒"
            event_id = emit_event(db, event_type="news.policy_effective",
                dedupe_key=f"news:policy_effective:{item.id}:{reminder.effective_date.isoformat()}",
                title=f"政策日期提醒 · {timing}：{redact_text(item.title)}",
                text=f"原文标示的施行日期：{reminder.effective_date.isoformat()}。{timing}，不表示刚刚生效。\n"
                     "此提醒不判断政策是否适用于本公司，请核对官方原文。\n" + redact_text(item.excerpt)[:500],
                subject_id=uuid.UUID(reminder.context["subject_id"]) if reminder.context.get("subject_id") else None,
                url=clean_url(item.url), news=news_metadata(item), recorded_at=at,
                policy={"reminder_id": str(reminder.id), "effective_date": reminder.effective_date.isoformat(),
                        "scheduled_for": reminder.scheduled_for.isoformat(), "timing": timing,
                        "basis_sha256": reminder.context["basis_sha256"]})
            if event_id is None:
                event_id = db.scalar(select(NotificationEvent.id).where(NotificationEvent.dedupe_key ==
                    f"news:policy_effective:{item.id}:{reminder.effective_date.isoformat()}"))
            else:
                created += 1
            reminder.event_id, reminder.status, reminder.error_code = event_id, "emitted", None
            db.commit()
    return created
