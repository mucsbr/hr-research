import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class AdapterRegistry(Entity, Base):
    __tablename__ = "adapter_registries"
    kind: Mapped[str] = mapped_column(String(30), unique=True)
    active_release_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey(
        "adapter_releases.id", use_alter=True, name="fk_adapter_active_release"))
    check_enabled: Mapped[bool] = mapped_column(default=False)
    agent_enabled: Mapped[bool] = mapped_column(default=False)
    auto_repair_enabled: Mapped[bool] = mapped_column(default=False)
    auto_publish_enabled: Mapped[bool] = mapped_column(default=False)
    code_agent_enabled: Mapped[bool] = mapped_column(default=False)
    code_auto_publish_enabled: Mapped[bool] = mapped_column(default=False)
    code_upstream_enabled: Mapped[bool] = mapped_column(default=False)
    release_generation: Mapped[int] = mapped_column(default=0)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upstream_commit: Mapped[str | None] = mapped_column(String(64))


class AdapterRelease(Entity, Base):
    __tablename__ = "adapter_releases"
    registry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("adapter_registries.id"), index=True)
    version: Mapped[str] = mapped_column(String(100))
    manifest: Mapped[dict] = mapped_column(JSONB)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="candidate")
    replaces_release_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("adapter_releases.id"))
    replaces_generation: Mapped[int | None] = mapped_column()
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)
    origin: Mapped[str] = mapped_column(String(40), default="manual")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


class AdapterMaintenanceRun(Entity, Base):
    __tablename__ = "adapter_maintenance_runs"
    registry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("adapter_registries.id"), index=True)
    release_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("adapter_releases.id"))
    kind: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), default="queued")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upstream_commit: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
