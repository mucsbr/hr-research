import uuid

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import Entity


class DiscoverySearchConfig(Entity, Base):
    __tablename__ = "discovery_search_configs"
    name: Mapped[str] = mapped_column(String(80), unique=True, default="default")
    endpoint_url: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(default=True)
    max_results: Mapped[int] = mapped_column(default=5)


class RecruitmentEntryCandidate(Entity, Base):
    __tablename__ = "recruitment_entry_candidates"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30), default="careers")
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    bound_source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("source_bindings.id"))
    __table_args__ = (UniqueConstraint("company_id", "url"),)


class JobMatchProposal(Entity, Base):
    __tablename__ = "job_match_proposals"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    left_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    right_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    confidence: Mapped[int] = mapped_column(default=0)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("left_job_id", "right_job_id"),)


class JobMergeRecord(Entity, Base):
    __tablename__ = "job_merge_records"
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), index=True)
    target_job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id"), index=True)
    merged_job_ids: Mapped[list] = mapped_column(JSONB, default=list)
    record_moves: Mapped[list] = mapped_column(JSONB, default=list)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="merged")
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    undone_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
