import hashlib
import uuid
from datetime import timedelta

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.llm import shanghai_day_start
from app.models import (
    AuditEvent, Company, Job, JobChange, JobSourceRecord, JobVersion, ModelCall, MonitoringPlan,
    Report, RunBatch, SourceAccount, SourceBinding, SourceRun, Subject, TaskEvent, TaskRun,
    User, UserSession, now,
)
from app.schemas import (
    AccountInput, CompanyInput, CompanyPatch, LoginInput, PlanInput, RunInput, SourceInput,
)
from app.security import AdminUser, CurrentUser, Db, new_session, verify_password
from app.serialization import row
from app.tasks import batch_state, create_analysis, create_batch, next_schedule, request_with_key

router = APIRouter(prefix="/api/v1")


def require(db, model, identifier):
    obj = db.get(model, identifier)
    if obj is None:
        raise HTTPException(404, "记录不存在")
    return obj


@router.post("/auth/login")
def login(data: LoginInput, response: Response, db: Db):
    user = db.scalar(select(User).where(User.username == data.username, User.active.is_(True)))
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    token = new_session(db, user)
    response.set_cookie("hr_session", token, httponly=True, samesite="lax",
                        secure=get_settings().cookie_secure,
                        max_age=get_settings().session_hours * 3600)
    return {"id": str(user.id), "username": user.username, "role": user.role}


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Db):
    token = request.cookies.get("hr_session", "")
    db.execute(delete(UserSession).where(
        UserSession.token_hash == hashlib.sha256(token.encode()).hexdigest()
    ))
    db.commit()
    response.delete_cookie("hr_session")
    return {"ok": True}


@router.get("/auth/me")
def me(user: CurrentUser):
    return {"id": str(user.id), "username": user.username, "role": user.role}


@router.get("/companies")
def companies(db: Db, user: CurrentUser, archived: bool = False):
    return [row(c) for c in db.scalars(
        select(Company).where(Company.archived == archived).order_by(Company.created_at.desc())
    )]


@router.get("/subjects")
def subjects(db: Db, user: CurrentUser):
    return [row(s) for s in db.scalars(select(Subject).where(Subject.active.is_(True))
                                     .order_by(Subject.name))]


@router.post("/companies", status_code=201)
def add_company(data: CompanyInput, db: Db, user: CurrentUser):
    subject = Subject(name=data.name, aliases=data.aliases)
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, **data.model_dump(exclude={"aliases"}))
    db.add(company)
    db.flush()
    db.add(AuditEvent(user_id=user.id, action="company.created", target=str(company.id)))
    db.commit()
    return row(company)


@router.get("/companies/{company_id}")
def get_company(company_id: uuid.UUID, db: Db, user: CurrentUser):
    company = require(db, Company, company_id)
    result = row(company)
    result["aliases"] = require(db, Subject, company.subject_id).aliases
    result["sources"] = [row(s) for s in db.scalars(
        select(SourceBinding).where(SourceBinding.company_id == company_id))]
    return result


@router.patch("/companies/{company_id}")
def update_company(company_id: uuid.UUID, data: CompanyPatch, db: Db, user: CurrentUser):
    company = require(db, Company, company_id)
    subject = require(db, Subject, company.subject_id)
    values = data.model_dump(exclude_unset=True)
    if "aliases" in values:
        subject.aliases = values.pop("aliases") or []
    for key, value in values.items():
        if value is None and key in {"name", "role", "notes", "archived"}:
            raise HTTPException(422, f"{key} 不能为空")
        setattr(company, key, value)
    subject.name = company.name
    db.commit()
    return row(company)


@router.get("/source-bindings")
def source_bindings(db: Db, user: CurrentUser, company_id: uuid.UUID | None = None):
    query = select(SourceBinding)
    if company_id:
        query = query.where(SourceBinding.company_id == company_id)
    return [row(s) for s in db.scalars(query.order_by(SourceBinding.created_at.desc()))]


@router.post("/source-bindings", status_code=201)
def add_source(data: SourceInput, db: Db, user: CurrentUser):
    require(db, Company, data.company_id)
    if data.account_id:
        account = require(db, SourceAccount, data.account_id)
        if account.kind != data.kind:
            raise HTTPException(422, "账号平台与数据源不匹配")
    if data.adapter == "tencent" and data.kind != "careers":
        raise HTTPException(422, "腾讯官网适配器仅用于招聘官网")
    binding = SourceBinding(**data.model_dump())
    db.add(binding)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "这家公司已经绑定同一来源网址") from exc
    return row(binding)


@router.get("/source-accounts")
def accounts(db: Db, user: CurrentUser, include_archived: bool = False):
    from app.probe_service import snapshot_for_account
    query = select(SourceAccount).order_by(SourceAccount.created_at.desc())
    if not include_archived:
        query = query.where(SourceAccount.archived.is_(False))
    return [{**row(a, exclude={"profile_ref", "session_ref"}),
             "probe": snapshot_for_account(db, a.id)} for a in db.scalars(query)]


@router.post("/source-accounts", status_code=201)
def add_account(data: AccountInput, db: Db, user: AdminUser):
    account = SourceAccount(**data.model_dump())
    db.add(account)
    db.commit()
    return row(account, exclude={"profile_ref", "session_ref"})


@router.post("/recruitment/runs", status_code=202)
def run(data: RunInput, db: Db, user: CurrentUser,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200)):
    batch, created = create_batch(db, request_with_key(data, idempotency_key))
    return {"batch_id": str(batch.id), "created": created}


@router.post("/companies/{company_id}/recruitment-runs", status_code=202)
def run_company(company_id: uuid.UUID, db: Db, user: CurrentUser,
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200)):
    batch, created = create_batch(db, request_with_key(RunInput(company_ids=[company_id]), idempotency_key))
    return {"batch_id": str(batch.id), "created": created}


@router.get("/batches")
def batches(db: Db, user: CurrentUser, limit: int = Query(30, ge=1, le=100)):
    result = []
    for batch in db.scalars(select(RunBatch).order_by(RunBatch.created_at.desc()).limit(limit)):
        status, counts = batch_state(db, batch.id)
        result.append({**row(batch), "status": status, "counts": counts})
    return result


@router.get("/batches/{batch_id}")
def get_batch(batch_id: uuid.UUID, db: Db, user: CurrentUser):
    batch = require(db, RunBatch, batch_id)
    status, counts = batch_state(db, batch_id)
    tasks = db.scalars(select(TaskRun).where(TaskRun.batch_id == batch_id)).all()
    return {**row(batch), "status": status, "counts": counts, "tasks": [row(t) for t in tasks]}


@router.get("/tasks")
def tasks(db: Db, user: CurrentUser, company_id: uuid.UUID | None = None,
          limit: int = Query(50, ge=1, le=100)):
    query = select(TaskRun).order_by(TaskRun.created_at.desc()).limit(limit)
    if company_id:
        query = query.where(TaskRun.company_id == company_id)
    result = []
    for task in db.scalars(query):
        snapshot = task.config_snapshot
        brief = {key: snapshot[key] for key in ("company_name", "name", "days") if key in snapshot}
        if snapshot.get("subject"):
            brief["subject"] = {key: snapshot["subject"].get(key) for key in ("id", "name", "type")}
        if task.kind == "company_comparison":
            brief["name"] = "竞品招聘比较 · " + "、".join(snapshot.get("company_names", {}).values())
        if task.domain == "news":
            brief["name"] = "HR 资讯采集"
        if task.domain == "maintenance":
            brief["name"] = "适配器维护 · " + snapshot.get("adapter_kind", "")
        result.append({**row(task, exclude={"config_snapshot"}), "config_snapshot": brief})
    return result


@router.get("/tasks/{task_id}")
def task(task_id: uuid.UUID, db: Db, user: CurrentUser):
    obj = require(db, TaskRun, task_id)
    if obj.domain == "sentiment":
        from app.sentiment_api import get_run
        return get_run(task_id, db, user)
    if obj.domain == "news":
        from app.news_api import get_run
        return get_run(task_id, db, user)
    sources = db.scalars(select(SourceRun).where(SourceRun.task_id == task_id)).all()
    from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
    checkpoints = {}
    for checkpoint, steps, first_at in db.execute(select(
        OfficialCheckpoint, func.count(OfficialCheckpointStep.id), func.min(OfficialCheckpointStep.captured_at),
    ).outerjoin(OfficialCheckpointStep, OfficialCheckpointStep.checkpoint_id == OfficialCheckpoint.id)
         .where(OfficialCheckpoint.task_id == task_id).group_by(OfficialCheckpoint.id)):
        expires_at = (first_at or checkpoint.created_at) + timedelta(seconds=get_settings().checkpoint_max_age_seconds)
        checkpoints[checkpoint.binding_id] = {"id": str(checkpoint.id), "status": checkpoint.status,
            "saved_pages": steps, "raw_bytes": checkpoint.raw_bytes, "expires_at": expires_at.isoformat(),
            "within_time_limit": expires_at > now(), "source_configuration_rechecked_on_resume": True}
    events = db.scalars(select(TaskEvent).where(TaskEvent.task_id == task_id)
                       .order_by(TaskEvent.created_at).limit(500)).all()
    return {**row(obj), "sources": [{**row(s), "checkpoint": checkpoints.get(s.binding_id)} for s in sources],
            "events": [row(e) for e in events]}


@router.post("/tasks/{task_id}/cancel")
def cancel(task_id: uuid.UUID, db: Db, user: CurrentUser):
    obj = require(db, TaskRun, task_id)
    if obj.status in {"succeeded", "failed", "partial", "cancelled"}:
        return row(obj)
    obj.cancel_requested = True
    if obj.status != "running":
        obj.status = "cancelled"
        obj.finished_at = now()
    db.commit()
    return row(obj)


@router.post("/tasks/{task_id}/retry", status_code=202)
def retry(task_id: uuid.UUID, db: Db, user: CurrentUser, resume: bool = True):
    old = require(db, TaskRun, task_id)
    if old.domain == "sentiment":
        from app.sentiment_api import retry_run
        return retry_run(task_id, db, user)
    if old.domain == "news":
        from app.news_api import retry_run
        return retry_run(task_id, db, user)
    if old.domain == "maintenance":
        raise HTTPException(409, "请到适配器维护页核对结果并重新发起；候选生成可能使用模型预算")
    if old.status in {"running", "queued"}:
        raise HTTPException(409, "任务仍在执行")
    if old.kind == "company_analysis":
        task, created = create_analysis(db, old.company_id)
        return {"task_id": str(task.id), "created": created}
    if old.kind == "company_comparison":
        from app.analytics_service import create_comparison
        task, created = create_comparison(db, [uuid.UUID(value) for value in old.config_snapshot["company_ids"]],
                                          days=old.config_snapshot["days"])
        return {"task_id": str(task.id), "created": created}
    # A new run preserves evidence and versions of the original execution.
    snapshot = old.config_snapshot
    from app.schemas import ScopeFilters
    filters = ScopeFilters.model_validate({k: v for k, v in snapshot.items()
                                          if k in ScopeFilters.model_fields})
    batch, created = create_batch(db, RunInput(
        name="重新采集", company_ids=[old.company_id], filters=filters
    ), trigger="retry", resume_from_task_id=old.id if resume else None)
    return {"batch_id": str(batch.id), "previous_task_id": str(old.id), "created": created,
            "resume_requested": resume}


@router.get("/plans")
def plans(db: Db, user: CurrentUser):
    return [row(p) for p in db.scalars(select(MonitoringPlan).order_by(
        MonitoringPlan.created_at.desc()))]


@router.post("/plans", status_code=201)
def add_plan(data: PlanInput, db: Db, user: CurrentUser):
    for identifier in data.company_ids:
        require(db, Company, identifier)
    if data.enabled and not data.schedule:
        raise HTTPException(422, "启用定时计划需要执行时间")
    next_at = next_schedule(data.schedule, data.timezone) if data.schedule else None
    plan = MonitoringPlan(
        name=data.name, company_ids=[str(x) for x in dict.fromkeys(data.company_ids)],
        filters=data.filters.model_dump(), schedule=data.schedule, enabled=data.enabled,
        timezone=data.timezone, next_run_at=next_at if data.enabled else None,
    )
    db.add(plan)
    db.commit()
    return row(plan)


@router.post("/plans/{plan_id}/pause")
def pause_plan(plan_id: uuid.UUID, db: Db, user: CurrentUser):
    plan = require(db, MonitoringPlan, plan_id)
    plan.enabled = False
    plan.next_run_at = None
    db.commit()
    return row(plan)


@router.post("/plans/{plan_id}/run", status_code=202)
def run_plan(plan_id: uuid.UUID, db: Db, user: CurrentUser,
             idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200)):
    plan = require(db, MonitoringPlan, plan_id)
    batch, created = create_batch(db, request_with_key(RunInput(
        name=plan.name, company_ids=plan.company_ids, filters=plan.filters
    ), idempotency_key), plan=plan)
    return {"batch_id": str(batch.id), "created": created}


@router.get("/jobs")
def jobs(db: Db, user: CurrentUser, company_id: uuid.UUID | None = None,
         q: str = "", status: str | None = None, page: int = Query(1, ge=1),
         page_size: int = Query(25, ge=1, le=100)):
    query = select(Job).where(Job.status != "merged")
    if company_id:
        query = query.where(Job.company_id == company_id)
    if q:
        query = query.where(Job.title.ilike("%" + q + "%"))
    if status:
        query = query.where(Job.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = []
    for job in db.scalars(query.order_by(Job.updated_at.desc())
                         .offset((page - 1) * page_size).limit(page_size)):
        records = db.scalars(select(JobSourceRecord).where(JobSourceRecord.job_id == job.id)).all()
        items.append({**row(job), "sources": [row(r) for r in records]})
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/jobs/{job_id}")
def get_job(job_id: uuid.UUID, db: Db, user: CurrentUser):
    job = require(db, Job, job_id)
    records = db.scalars(select(JobSourceRecord).where(JobSourceRecord.job_id == job.id)).all()
    ids = [r.id for r in records]
    versions = db.scalars(select(JobVersion).where(JobVersion.record_id.in_(ids))
                         .order_by(JobVersion.created_at.desc())).all()
    changes = db.scalars(select(JobChange).where(JobChange.record_id.in_(ids))
                        .order_by(JobChange.created_at.desc())).all()
    return {**row(job), "sources": [row(r) for r in records],
            "versions": [row(v) for v in versions], "changes": [row(c) for c in changes]}


@router.get("/job-changes")
def changes(db: Db, user: CurrentUser, company_id: uuid.UUID | None = None,
            limit: int = Query(100, ge=1, le=200)):
    query = select(JobChange, Job.title, Job.company_id).join(
        JobSourceRecord, JobChange.record_id == JobSourceRecord.id
    ).join(Job, JobSourceRecord.job_id == Job.id)
    if company_id:
        query = query.where(Job.company_id == company_id)
    return [{**row(change), "title": title, "company_id": str(cid)}
            for change, title, cid in db.execute(query.order_by(
                JobChange.created_at.desc()).limit(limit))]


@router.get("/reports")
def reports(db: Db, user: CurrentUser):
    return [row(r) for r in db.scalars(select(Report).order_by(Report.created_at.desc()).limit(50))]


@router.get("/reports/{report_id}")
def report(report_id: uuid.UUID, db: Db, user: CurrentUser):
    return row(require(db, Report, report_id))


@router.post("/companies/{company_id}/analysis", status_code=202)
def company_analysis(company_id: uuid.UUID, db: Db, user: CurrentUser):
    task, created = create_analysis(db, company_id)
    return {"task_id": str(task.id), "created": created}


@router.get("/dashboard")
def dashboard(db: Db, user: CurrentUser):
    from app.news_models import NewsItem
    from app.sentiment_models import SentimentItem
    def count(model):
        return db.scalar(select(func.count()).select_from(model))
    return {
        "companies": db.scalar(select(func.count()).select_from(Company).where(~Company.archived)),
        "jobs": db.scalar(select(func.count()).select_from(Job).where(Job.status != "merged")),
        "sources": count(SourceBinding),
        "sentiment_items": db.scalar(select(func.count()).select_from(SentimentItem).where(
            SentimentItem.is_ignored.is_(False))),
        "unread_news": db.scalar(select(func.count()).select_from(NewsItem).where(NewsItem.is_read.is_(False))),
        "reports": count(Report),
        "active_subjects": db.scalar(select(func.count()).select_from(Subject).where(Subject.active.is_(True))),
        "active_tasks": db.scalar(select(func.count()).select_from(TaskRun).where(
            TaskRun.status.in_(["queued", "running"]))),
        "needs_action": db.scalar(select(func.count()).select_from(TaskRun).where(
            TaskRun.status.in_(["waiting_login", "waiting_verification", "waiting_device", "blocked_configuration"]))),
        "changes_7d": db.scalar(select(func.count()).select_from(JobChange).where(
            JobChange.created_at > now() - timedelta(days=7), JobChange.type != "baseline_seen")),
        "latest_completed_at": db.scalar(select(func.max(TaskRun.finished_at))),
    }


@router.get("/system/status")
def system_status(db: Db, user: CurrentUser):
    from app.model_settings import effective_configuration, ModelConfigurationError
    try:
        settings, _, _ = effective_configuration(db)
    except ModelConfigurationError as error:
        raise HTTPException(503, str(error)) from None
    db.execute(text("SELECT 1"))
    return {
        "database": "ready",
        "llm": {"configured": bool(settings.llm_base_url and settings.llm_model and settings.llm_api_key.get_secret_value()),
                "model": settings.llm_model, "daily_request_limit": settings.llm_daily_request_limit,
                "task_call_limit": settings.llm_task_call_limit,
                "max_output_tokens": settings.llm_max_output_tokens,
                "calls_today": db.scalar(select(func.count()).select_from(ModelCall).where(
                    ModelCall.created_at >= shanghai_day_start()))},
        "bettafish": {"configured": bool(settings.bettafish_repo_dir and settings.bettafish_python
                                          and settings.bettafish_license_confirmed)},
        "timezone": "Asia/Shanghai",
    }
