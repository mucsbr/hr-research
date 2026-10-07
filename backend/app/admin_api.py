import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.models import AuditEvent, MonitoringPlan, User, UserSession
from app.schemas import Input, PlanInput
from app.security import AdminUser, CurrentUser, Db, hasher
from app.serialization import row
from app.tasks import next_schedule

router = APIRouter(prefix="/api/v1")


class UserInput(Input):
    username: str = Field(min_length=1, max_length=100, pattern=r"^[\w.@-]+$")
    password: SecretStr = Field(min_length=12, max_length=500)
    role: Literal["admin", "user"] = "user"


class UserPatch(Input):
    active: bool | None = None
    role: Literal["admin", "user"] | None = None


@router.get("/users")
def users(db: Db, user: AdminUser):
    return [row(item, exclude={"password_hash"}) for item in db.scalars(select(User).order_by(User.username))]


@router.post("/users", status_code=201)
def create_user(data: UserInput, db: Db, user: AdminUser):
    account = User(username=data.username, password_hash=hasher.hash(data.password.get_secret_value()), role=data.role)
    db.add(account)
    try:
        db.flush()
        db.add(AuditEvent(user_id=user.id, action="user.created", target=str(account.id),
                          detail={"role": data.role}))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "用户名已存在") from None
    return row(account, exclude={"password_hash"})


@router.patch("/users/{user_id}")
def patch_user(user_id: uuid.UUID, data: UserPatch, db: Db, user: AdminUser):
    values = data.model_dump(exclude_unset=True)
    if not values or any(value is None for value in values.values()):
        raise HTTPException(422, "请提供启用状态或角色")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('admin-membership', 0))"))
    account = db.get(User, user_id)
    if not account:
        raise HTTPException(404, "用户不存在")
    if account.active and account.role == "admin" and (
        values.get("active") is False or values.get("role", "admin") != "admin"
    ):
        administrators = db.scalar(select(func.count()).select_from(User).where(
            User.active.is_(True), User.role == "admin"))
        if administrators <= 1:
            raise HTTPException(409, "至少保留一个启用的管理员")
    for key, value in values.items():
        setattr(account, key, value)
    # Role changes must also terminate old sessions, including a previously logged-in admin.
    db.execute(delete(UserSession).where(UserSession.user_id == account.id))
    db.add(AuditEvent(user_id=user.id, action="user.updated", target=str(account.id), detail=values))
    db.commit()
    return row(account, exclude={"password_hash"})


@router.put("/plans/{plan_id}")
def update_plan(plan_id: uuid.UUID, data: PlanInput, db: Db, user: CurrentUser):
    from app.models import Company
    plan = db.scalar(select(MonitoringPlan).where(MonitoringPlan.id == plan_id).with_for_update())
    if not plan:
        raise HTTPException(404, "计划不存在")
    for identifier in set(data.company_ids):
        company = db.get(Company, identifier)
        if not company or company.archived:
            raise HTTPException(422, "部分公司不存在或已归档")
    if data.enabled and not data.schedule:
        raise HTTPException(422, "启用定时需要设置时间")
    plan.name, plan.company_ids = data.name, [str(value) for value in dict.fromkeys(data.company_ids)]
    plan.filters, plan.schedule, plan.timezone = data.filters.model_dump(), data.schedule, data.timezone
    plan.enabled = data.enabled
    # Validate schedules even when currently disabled, so re-enabling is deterministic.
    next_at = next_schedule(data.schedule, data.timezone) if data.schedule else None
    plan.next_run_at = next_at if data.enabled else None
    db.add(AuditEvent(user_id=user.id, action="plan.updated", target=str(plan.id)))
    db.commit()
    return row(plan)


@router.get("/audit-events")
def audit_events(db: Db, user: AdminUser):
    return [row(event) for event in db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(100))]
