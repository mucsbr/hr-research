"""Persistent outbound browser devices. Secrets and browser payloads are never public rows."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class ProbePairing(Entity, Base):
    __tablename__ = "probe_pairings"
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProbeDevice(Entity, Base):
    __tablename__ = "probe_devices"
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    platform: Mapped[str] = mapped_column(String(20))
    architecture: Mapped[str] = mapped_column(String(20))
    version: Mapped[str] = mapped_column(String(40))
    protocol_version: Mapped[str] = mapped_column(String(20), default="probe_v1")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    capabilities: Mapped[list] = mapped_column(JSONB, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    paused: Mapped[bool] = mapped_column(default=False)
    connection_id: Mapped[uuid.UUID | None] = mapped_column()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    chrome_version: Mapped[str | None] = mapped_column(String(100))


class ProbeProfile(Entity, Base):
    __tablename__ = "probe_profiles"
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("probe_devices.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(default=True)


class ProbeAccountBinding(Entity, Base):
    __tablename__ = "probe_account_bindings"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_accounts.id"), unique=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("probe_profiles.id"), index=True)
    revision: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)


class ProbeBrowserSession(Entity, Base):
    __tablename__ = "probe_browser_sessions"
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("probe_profiles.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_accounts.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"), index=True)
    attempt: Mapped[int | None] = mapped_column()
    kind: Mapped[str] = mapped_column(String(30))
    binding_revision: Mapped[uuid.UUID] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "uq_probe_active_profile",
            "profile_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class ProbeCommand(Entity, Base):
    __tablename__ = "probe_commands"
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("probe_devices.id"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("probe_browser_sessions.id"), index=True
    )
    operation: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    payload_cipher: Mapped[str | None] = mapped_column(Text)
    result_cipher: Mapped[str | None] = mapped_column(Text)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(80))
    connection_id: Mapped[uuid.UUID | None] = mapped_column()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_probe_command_dispatch", "device_id", "status", "created_at"),)
