import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class AdapterRepairIncident(Entity, Base):
    __tablename__ = "adapter_repair_incidents"
    registry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("adapter_registries.id"), index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_runs.id"), index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_bindings.id"), index=True)
    maintenance_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    recovery_task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    release_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("adapter_releases.id"))
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    error_code: Mapped[str] = mapped_column(String(100))
    base_fingerprint: Mapped[str] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    recovery_attempts: Mapped[int] = mapped_column(default=0)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
