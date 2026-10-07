"""Bounded official-site collection checkpoints; raw bodies live outside the database."""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class OfficialCheckpoint(Entity, Base):
    __tablename__ = "official_checkpoints"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_bindings.id"))
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_runs.id"))
    attempt: Mapped[int] = mapped_column()
    scope_fingerprint: Mapped[str] = mapped_column(String(64))
    binding_fingerprints: Mapped[list] = mapped_column(JSONB, default=list)
    plan: Mapped[dict] = mapped_column(JSONB, default=dict)
    plan_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    state: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="collecting")
    raw_bytes: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("task_id", "binding_id"),)


class OfficialCheckpointStep(Entity, Base):
    __tablename__ = "official_checkpoint_steps"
    checkpoint_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("official_checkpoints.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(64))
    requested_url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64))
    storage_ref: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column()
    kind: Mapped[str] = mapped_column(String(24))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parsed_records: Mapped[list] = mapped_column(JSONB, default=list)
    step_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("checkpoint_id", "request_key"),)
