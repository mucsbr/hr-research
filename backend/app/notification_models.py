import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity, now


class NotificationChannel(Entity, Base):
    __tablename__ = "notification_channels"
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(20))
    encrypted_config: Mapped[str] = mapped_column(Text)
    destination_hint: Mapped[str] = mapped_column(String(250))
    enabled: Mapped[bool] = mapped_column(default=False)


class NotificationRule(Entity, Base):
    __tablename__ = "notification_rules"
    name: Mapped[str] = mapped_column(String(120))
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channels.id"), index=True)
    event_types: Mapped[list] = mapped_column(JSONB, default=list)
    subject_ids: Mapped[list] = mapped_column(JSONB, default=list)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    news_filters: Mapped[dict] = mapped_column(JSONB, default=lambda: {"regions": [], "industries": [], "topics": []})
    cooldown_seconds: Mapped[int] = mapped_column(default=0)
    delivery_mode: Mapped[str] = mapped_column(String(20), default="immediate")
    digest_time: Mapped[str] = mapped_column(String(5), default="09:00")
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Shanghai")
    last_queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(default=False)


class NotificationEvent(Entity, Base):
    __tablename__ = "notification_events"
    type: Mapped[str] = mapped_column(String(100), index=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("watch_subjects.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    dedupe_key: Mapped[str] = mapped_column(String(250), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class NotificationDelivery(Entity, Base):
    __tablename__ = "notification_deliveries"
    event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_events.id"), index=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channels.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint("event_id", "channel_id"),)


class NotificationDigestItem(Entity, Base):
    __tablename__ = "notification_digest_items"
    rule_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_rules.id"), index=True)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_channels.id"))
    channel_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_event_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notification_events.id"))
    digest_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("notification_events.id"))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    __table_args__ = (
        UniqueConstraint("rule_id", "source_event_id"),
        Index("ix_notification_digest_items_due", "status", "scheduled_for"),
    )


class NewsPolicyReminder(Entity, Base):
    __tablename__ = "news_policy_reminders"
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("news_items.id"), index=True)
    effective_date: Mapped[date] = mapped_column(Date)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("notification_events.id"))
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("item_id", "effective_date"),
        Index("ix_news_policy_reminders_due", "status", "scheduled_for"),
    )
