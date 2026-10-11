import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now():
    return datetime.now(timezone.utc)


class Entity:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class User(Entity, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20), default="admin")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class UserSession(Entity, Base):
    __tablename__ = "user_sessions"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Subject(Entity, Base):
    __tablename__ = "watch_subjects"
    type: Mapped[str] = mapped_column(String(30), default="company")
    name: Mapped[str] = mapped_column(String(250))
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    keywords: Mapped[list] = mapped_column(JSONB, default=list)
    exclusions: Mapped[list] = mapped_column(JSONB, default=list)
    active: Mapped[bool] = mapped_column(default=True)


class Company(Entity, Base):
    __tablename__ = "companies"
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("watch_subjects.id"), unique=True)
    name: Mapped[str] = mapped_column(String(250))
    official_name: Mapped[str | None] = mapped_column(String(250))
    role: Mapped[str] = mapped_column(String(30), default="competitor")
    website: Mapped[str | None] = mapped_column(Text)
    identity_status: Mapped[str] = mapped_column(String(30), default="user_specified")
    scope: Mapped[dict] = mapped_column(JSONB, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(default=False)


class SourceAccount(Entity, Base):
    __tablename__ = "source_accounts"
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    archived: Mapped[bool] = mapped_column(default=False)
    health: Mapped[str] = mapped_column(String(40), default="logged_out")
    profile_ref: Mapped[str | None] = mapped_column(Text)
    session_ref: Mapped[str | None] = mapped_column(Text)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BrowserLoginSession(Entity, Base):
    __tablename__ = "login_sessions"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_accounts.id"), index=True)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="starting")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class SourceBinding(Entity, Base):
    __tablename__ = "source_bindings"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    entry_url: Mapped[str] = mapped_column(Text)
    external_company_id: Mapped[str | None] = mapped_column(String(200))
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source_accounts.id"))
    adapter: Mapped[str] = mapped_column(String(100), default="generic")
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    health: Mapped[str] = mapped_column(String(40), default="unknown")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("company_id", "kind", "entry_url"),)


class MonitoringPlan(Entity, Base):
    __tablename__ = "monitoring_plans"
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(30), default="recruitment")
    company_ids: Mapped[list] = mapped_column(JSONB, default=list)
    filters: Mapped[dict] = mapped_column(JSONB, default=dict)
    schedule: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    enabled: Mapped[bool] = mapped_column(default=False)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class RunBatch(Entity, Base):
    __tablename__ = "run_batches"
    plan_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("monitoring_plans.id"))
    name: Mapped[str] = mapped_column(String(200))
    trigger: Mapped[str] = mapped_column(String(30), default="manual")
    status: Mapped[str] = mapped_column(String(40), default="queued")
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)


class TaskRun(Entity, Base):
    __tablename__ = "task_runs"
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("run_batches.id"), index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"), index=True)
    domain: Mapped[str] = mapped_column(String(30), default="recruitment")
    kind: Mapped[str] = mapped_column(String(40), default="company_collection")
    status: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    scope_hash: Mapped[str] = mapped_column(String(64), default="")
    progress: Mapped[int] = mapped_column(default=0)
    attempt: Mapped[int] = mapped_column(default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    logs_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    log_retention: Mapped[dict] = mapped_column(JSONB, default=dict)


class CompanyRun(Base):
    __tablename__ = "company_runs"
    task_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    scope_hash: Mapped[str] = mapped_column(String(64))
    coverage: Mapped[str] = mapped_column(String(20), default="unknown")


class SourceRun(Entity, Base):
    __tablename__ = "source_runs"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_bindings.id"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="queued")
    coverage: Mapped[str] = mapped_column(String(20), default="unknown")
    scope_hash: Mapped[str] = mapped_column(String(64))
    adapter_version: Mapped[str] = mapped_column(String(100), default="builtin-0.1")
    counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("task_id", "binding_id"),)


class TaskEvent(Entity, Base):
    __tablename__ = "task_events"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    level: Mapped[str] = mapped_column(String(20), default="info")
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


class RawDocument(Entity, Base):
    __tablename__ = "raw_documents"
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("task_runs.id"), index=True)
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_ref: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(100), default="text/html")
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retention: Mapped[dict] = mapped_column(JSONB, default=dict)


class Job(Entity, Base):
    __tablename__ = "jobs"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    title: Mapped[str] = mapped_column(String(500))
    locations: Mapped[list] = mapped_column(JSONB, default=list)
    function: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(40), default="observed")


class JobSourceRecord(Entity, Base):
    __tablename__ = "job_source_records"
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_bindings.id"), index=True)
    source_job_id: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(Text)
    recruiting_entity: Mapped[str | None] = mapped_column(String(250))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    current_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="observed")
    missing_count: Mapped[int] = mapped_column(default=0)
    baseline_scope: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("binding_id", "source_job_id"),)


class JobVersion(Entity, Base):
    __tablename__ = "job_versions"
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_source_records.id"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSONB)
    raw_document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_documents.id"))
    __table_args__ = (UniqueConstraint("record_id", "content_hash"),)


class JobObservation(Entity, Base):
    __tablename__ = "job_observations"
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_source_records.id"), index=True)
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_runs.id"), index=True)
    version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_versions.id"))
    __table_args__ = (UniqueConstraint("record_id", "source_run_id"),)


class JobChange(Entity, Base):
    __tablename__ = "job_change_events"
    record_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("job_source_records.id"), index=True)
    source_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("source_runs.id"), index=True)
    type: Mapped[str] = mapped_column(String(40), index=True)
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("record_id", "source_run_id", "type"),)


class Report(Entity, Base):
    __tablename__ = "analysis_reports"
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"))
    domain: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(250))
    content: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    coverage: Mapped[dict] = mapped_column(JSONB, default=dict)
    model: Mapped[str | None] = mapped_column(String(100))
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    retention: Mapped[dict] = mapped_column(JSONB, default=dict)


class ModelCall(Entity, Base):
    __tablename__ = "model_calls"
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("task_runs.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    input_chars: Mapped[int] = mapped_column(default=0)
    max_output_tokens: Mapped[int] = mapped_column(default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    finish_reason: Mapped[str | None] = mapped_column(String(40))
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80))


class AuditEvent(Entity, Base):
    __tablename__ = "audit_events"
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(250))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


Index("ix_model_calls_created_status", ModelCall.created_at, ModelCall.status)
