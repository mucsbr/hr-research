"""Reputation records are separate from recruitment jobs and preserve source coverage."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity, now


class SentimentSource(Entity, Base):
    __tablename__ = "sentiment_sources"
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watch_subjects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="public_web")
    name: Mapped[str] = mapped_column(String(160))
    entry_urls: Mapped[list] = mapped_column(JSONB, default=list)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source_accounts.id"))
    enabled: Mapped[bool] = mapped_column(default=True)
    health: Mapped[str] = mapped_column(String(40), default="not_collected")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("subject_id", "name"),)


class SentimentSourceRun(Entity, Base):
    __tablename__ = "sentiment_source_runs"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sentiment_sources.id"), index=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watch_subjects.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="queued")
    coverage: Mapped[str] = mapped_column(String(40), default="unknown")
    pages: Mapped[int] = mapped_column(default=0)
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    limitations: Mapped[list] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("task_id", "source_id"),)


class SentimentItem(Entity, Base):
    __tablename__ = "sentiment_items"
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watch_subjects.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(250))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    polarity: Mapped[str] = mapped_column(String(30), default="unclassified")
    classification: Mapped[dict] = mapped_column(JSONB, default=dict)
    metrics: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_read: Mapped[bool] = mapped_column(default=False)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    is_ignored: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        UniqueConstraint("subject_id", "platform", "identity_hash"),
        Index("ix_sentiment_subject_last_seen", "subject_id", "last_seen_at"),
    )


class SentimentObservation(Entity, Base):
    __tablename__ = "sentiment_observations"
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sentiment_items.id"), index=True)
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sentiment_source_runs.id"), index=True)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_documents.id"))
    content_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("item_id", "source_run_id"),)
