"""HR news sources, original articles and their collection provenance."""
import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity, now


class NewsSource(Entity, Base):
    __tablename__ = "news_sources"
    name: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="feed")
    url: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("watch_subjects.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(30), default="other")
    publisher: Mapped[str | None] = mapped_column(String(200))
    regions: Mapped[list] = mapped_column(JSONB, default=list)
    topics: Mapped[list] = mapped_column(JSONB, default=list)
    industries: Mapped[list] = mapped_column(JSONB, default=list)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    exclusions: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    health: Mapped[str] = mapped_column(String(40), default="not_collected")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NewsSourceRun(Entity, Base):
    __tablename__ = "news_source_runs"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("news_sources.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    coverage: Mapped[str] = mapped_column(String(40), default="unknown")
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    limitations: Mapped[list] = mapped_column(JSONB, default=list)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("task_id", "source_id"),)


class NewsItem(Entity, Base):
    __tablename__ = "news_items"
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(600))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    excerpt: Mapped[str] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(30), default="other")
    content_type: Mapped[str] = mapped_column(String(40), default="source_article", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_date: Mapped[date | None] = mapped_column(Date)
    policy_issued_date: Mapped[date | None] = mapped_column(Date)
    effective_date: Mapped[date | None] = mapped_column(Date)
    applicable_audience: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    regions: Mapped[list] = mapped_column(JSONB, default=list)
    topics: Mapped[list] = mapped_column(JSONB, default=list)
    industries: Mapped[list] = mapped_column(JSONB, default=list)
    classification: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_read: Mapped[bool] = mapped_column(default=False)
    is_favorite: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (Index("ix_news_published_seen", "published_date", "last_seen_at"),)


class NewsObservation(Entity, Base):
    __tablename__ = "news_observations"
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("news_items.id"), index=True)
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("news_source_runs.id"), index=True)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_documents.id"))
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("item_id", "source_run_id", "url_hash"),)
