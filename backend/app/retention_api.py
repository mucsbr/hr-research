"""Retention is readable internally; all configuration, previews and execution require admin."""
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, StrictInt, field_validator
from sqlalchemy import select

from app.retention_models import RetentionRun
from app.schemas import Input
from app.security import AdminUser, CurrentUser, Db

router = APIRouter(prefix="/api/v1/retention", tags=["retention"])
Category = Literal["raw_documents", "reports", "task_logs", "expired_sessions"]


class PolicyInput(Input):
    category: Category
    retention_days: StrictInt = Field(ge=1, le=3650)
    enabled: bool
    expected_revision: StrictInt = Field(ge=1)


class PolicyUpdate(Input):
    policies: list[PolicyInput] = Field(min_length=1, max_length=4)

    @field_validator("policies")
    @classmethod
    def unique_categories(cls, value):
        if len({item.category for item in value}) != len(value):
            raise ValueError("同一分类不能重复提交")
        return value


class PreviewInput(Input):
    categories: list[Category] = Field(default_factory=list, max_length=4)
    limit: StrictInt = Field(default=100, ge=1, le=200)


class RunInput(Input):
    preview_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=200)

    @field_validator("idempotency_key")
    @classmethod
    def valid_key(cls, value):
        if any(ord(character) < 32 for character in value):
            raise ValueError("幂等键不能包含控制字符")
        return value


@router.get("/policies")
def policies(db: Db, user: CurrentUser):
    from app.retention_service import policies_response
    return policies_response(db, can_manage=user.role == "admin")


@router.put("/policies")
def update_policies(data: PolicyUpdate, db: Db, user: AdminUser):
    from app.retention_service import change_policies, policies_response
    change_policies(db, [item.model_dump() for item in data.policies], user.id)
    return policies_response(db, can_manage=True)


@router.post("/preview")
def preview(data: PreviewInput, db: Db, user: AdminUser):
    from app.retention_service import create_preview, preview_response
    return preview_response(create_preview(db, categories=data.categories, limit=data.limit, actor_id=user.id))


@router.post("/runs", status_code=202)
def start_run(data: RunInput, db: Db, user: AdminUser):
    from app.retention_service import create_run, run_response
    run, created = create_run(db, data.preview_id, data.idempotency_key, actor_id=user.id)
    if created:
        try:
            from app.queue import celery
            celery.send_task("app.run_retention", kwargs={"run_id": str(run.id)}, queue="maintenance")
        except Exception:
            # The durable queued record is authoritative. Hourly dispatch can recover it.
            pass
    return run_response(run)


@router.get("/runs")
def runs(db: Db, user: CurrentUser, limit: int = Query(20, ge=1, le=100)):
    from app.retention_service import run_response
    return [run_response(run) for run in db.scalars(select(RetentionRun).order_by(RetentionRun.created_at.desc()).limit(limit))]


@router.get("/runs/by-key")
def run_by_key(db: Db, user: CurrentUser, idempotency_key: str = Query(min_length=1, max_length=200)):
    from app.retention_service import run_response
    run = db.scalar(select(RetentionRun).where(RetentionRun.idempotency_key == idempotency_key))
    if run is None:
        raise HTTPException(404, "尚未找到该幂等键的运行记录，请保留原请求核对")
    return run_response(run)


@router.get("/runs/{run_id}")
def get_run(run_id: uuid.UUID, db: Db, user: CurrentUser):
    from app.retention_service import run_response
    run = db.get(RetentionRun, run_id)
    if run is None:
        raise HTTPException(404, "清理运行不存在")
    return run_response(run)
