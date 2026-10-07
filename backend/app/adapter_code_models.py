"""Durable code-maintenance proposals; candidate bytes are never imported here."""
import uuid

from sqlalchemy import ForeignKey, String, Text, event, inspect
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class AdapterCodeProposal(Entity, Base):
    __tablename__ = "adapter_code_proposals"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), unique=True)
    registry_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("adapter_registries.id"), index=True)
    base_release_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("adapter_releases.id"))
    base_generation: Mapped[int] = mapped_column()
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    origin: Mapped[str] = mapped_column(String(40))
    source_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence_cases: Mapped[list] = mapped_column(JSONB, default=list)
    base_spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    draft_spec: Mapped[dict] = mapped_column(JSONB, default=dict)
    lock: Mapped[dict] = mapped_column(JSONB, default=dict)
    bundle_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    release_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("adapter_releases.id"))
    validation_run_refs: Mapped[list] = mapped_column(JSONB, default=list)
    candidate_history: Mapped[list] = mapped_column(JSONB, default=list)
    attempts: Mapped[int] = mapped_column(default=0)
    max_candidates: Mapped[int] = mapped_column(default=2)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)


class AdapterCodePackage(Entity, Base):
    __tablename__ = "adapter_code_packages"
    bundle_sha: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    file_ref: Mapped[str] = mapped_column(Text)
    lock_sha: Mapped[str] = mapped_column(String(64))
    files_manifest: Mapped[dict] = mapped_column(JSONB, default=dict)
    dependencies: Mapped[list] = mapped_column(JSONB, default=list)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)


@event.listens_for(AdapterCodePackage, "before_update")
def immutable_package(mapper, connection, target):
    if any(attr.history.has_changes() for attr in inspect(target).attrs):
        raise ValueError("代码包不可修改；变更必须生成新的内容摘要和候选")
