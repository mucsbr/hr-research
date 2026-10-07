"""Durable runtime invocations, distinct from candidate validation jobs."""
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class AdapterProgramExecution(Entity, Base):
    __tablename__ = "adapter_program_executions"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_runs.id"), index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_bindings.id"))
    owner_attempt: Mapped[int] = mapped_column()
    execution_key: Mapped[str] = mapped_column(String(64), unique=True)
    adapter_fingerprint: Mapped[str] = mapped_column(String(64))
    bundle_sha: Mapped[str] = mapped_column(ForeignKey("adapter_code_packages.bundle_sha"))
    operation: Mapped[str] = mapped_column(String(30))
    input_sha: Mapped[str] = mapped_column(String(64))
    request_sha: Mapped[str] = mapped_column(String(64))
    request: Mapped[dict] = mapped_column(JSONB)
    remote_run_id: Mapped[str | None] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(30), default="prepared", index=True)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
