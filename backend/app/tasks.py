import hashlib
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import HTTPException
from sqlalchemy import exists, select, text
from sqlalchemy.orm import Session

from app.models import Company, CompanyRun, Job, MonitoringPlan, RunBatch, SourceBinding, Subject, TaskEvent, TaskRun, now
from app.schemas import RunInput, ScopeFilters
from app.serialization import row


def scope_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def next_schedule(expression: str, timezone: str, after: datetime | None = None):
    try:
        zone = ZoneInfo(timezone)
        if not croniter.is_valid(expression):
            raise ValueError()
        return croniter(expression, (after or now()).astimezone(zone)).get_next(datetime)
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, "定时表达式或时区无效") from exc


def request_with_key(data: RunInput, header_key: str | None):
    if header_key is None:
        return data
    key = header_key.strip()
    if not key or len(key) > 200 or any(ord(character) < 32 for character in key):
        raise HTTPException(422, "Idempotency-Key 格式无效")
    if data.idempotency_key and data.idempotency_key != key:
        raise HTTPException(409, "请求头与正文的幂等键不一致")
    return data.model_copy(update={"idempotency_key": key})


def _normalized_filters(filters):
    snapshot = ScopeFilters.model_validate(filters).model_dump()
    for key in ("cities", "keywords", "exclusions", "sources"):
        snapshot[key] = sorted(set(snapshot[key]))
    return snapshot


def _batch_parameters(db, batch):
    saved = batch.config_snapshot.get("request_parameters_v1")
    if saved:
        return {"resume_from_task_id": None, **saved}
    # Previously created batches predate request fingerprints; reconstruct their immutable input.
    tasks = db.execute(select(TaskRun.company_id, TaskRun.config_snapshot).where(TaskRun.batch_id == batch.id,
                                                                              TaskRun.company_id.is_not(None))).all()
    filters = {key: value for key, value in batch.config_snapshot.items() if key in ScopeFilters.model_fields}
    predecessors = {snapshot.get("resume_from_task_id") for _, snapshot in tasks if snapshot.get("resume_from_task_id")}
    return {"company_ids": sorted({str(value) for value, _ in tasks}), "filters": _normalized_filters(filters),
            "plan_id": str(batch.plan_id) if batch.plan_id else None, "name": batch.name, "trigger": batch.trigger,
            "resume_from_task_id": next(iter(predecessors)) if len(predecessors) == 1 else None}


def _work_parameters(parameters):
    return {key: parameters[key] for key in ("company_ids", "filters", "plan_id")}


def create_batch(db: Session, data: RunInput, trigger="manual", plan: MonitoringPlan | None = None,
                 *, resume_from_task_id: uuid.UUID | None = None):
    ids = list(dict.fromkeys(data.company_ids))
    snapshot = data.filters.model_dump()
    parameters = {"company_ids": sorted(str(value) for value in ids), "filters": _normalized_filters(snapshot),
                  "plan_id": str(plan.id) if plan else None, "name": data.name, "trigger": trigger,
                  "resume_from_task_id": str(resume_from_task_id) if resume_from_task_id else None}
    work = _work_parameters(parameters)
    work_key = scope_digest(work)
    # All entry points share the work lock; keys additionally serialize cross-scope reuse/conflicts.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": "batch-work:" + work_key})
    if data.idempotency_key:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                   {"key": "batch:" + data.idempotency_key})
        existing = db.scalar(select(RunBatch).where(
            RunBatch.idempotency_key == data.idempotency_key
        ))
        if existing:
            if _batch_parameters(db, existing) != parameters:
                raise HTTPException(409, "这个幂等键已用于不同的公司、筛选、计划或请求参数")
            db.commit()
            return existing, False
    else:
        # Legacy clients without a key reuse only in-flight work, never completed historical runs.
        active = exists(select(TaskRun.id).where(TaskRun.batch_id == RunBatch.id,
                                                 TaskRun.status.in_(["queued", "running"])))
        candidates = db.scalars(select(RunBatch).where(active).order_by(RunBatch.created_at.desc())).all()
        for existing in candidates:
            if _work_parameters(_batch_parameters(db, existing)) == work:
                db.commit()
                return existing, False
    companies = db.scalars(select(Company).where(Company.id.in_(ids), ~Company.archived)).all()
    if len(companies) != len(ids):
        raise HTTPException(422, "部分公司不存在或已归档")
    if resume_from_task_id:
        predecessor = db.get(TaskRun, resume_from_task_id)
        if (not predecessor or predecessor.kind != "company_collection" or predecessor.domain != "recruitment"
                or len(ids) != 1 or predecessor.company_id != ids[0]):
            raise HTTPException(422, "恢复前驱必须是同一公司的招聘采集任务")
    batch = RunBatch(name=data.name, trigger=trigger, plan_id=plan.id if plan else None,
                     idempotency_key=data.idempotency_key,
                     config_snapshot={**snapshot, "request_parameters_v1": parameters})
    db.add(batch)
    db.flush()
    for company in companies:
        bindings = db.scalars(select(SourceBinding).where(
            SourceBinding.company_id == company.id, SourceBinding.enabled.is_(True)
        )).all()
        if snapshot["sources"]:
            bindings = [b for b in bindings if b.kind in snapshot["sources"]]
        config = {**snapshot, "company_name": company.name,
                  "company_identity": {"name": company.name, "official_name": company.official_name,
                                       "aliases": db.get(Subject, company.subject_id).aliases},
                  "sources_snapshot": [row(b) for b in bindings]}
        if resume_from_task_id:
            config["resume_from_task_id"] = str(resume_from_task_id)
        task = TaskRun(
            batch_id=batch.id, company_id=company.id,
            config_snapshot=config, scope_hash=scope_digest(snapshot),
            status="queued" if bindings else "blocked_configuration",
            error_message=None if bindings else "请先给这家公司添加招聘来源",
        )
        db.add(task)
        db.flush()
        db.add(CompanyRun(task_run_id=task.id, company_id=company.id, scope_hash=task.scope_hash))
        db.add(TaskEvent(task_id=task.id, message="任务已创建，每家公司独立执行"))
    db.commit()
    return batch, True


def batch_state(db: Session, batch_id: uuid.UUID):
    tasks = db.scalars(select(TaskRun).where(TaskRun.batch_id == batch_id)).all()
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task.status] = counts.get(task.status, 0) + 1
    if counts.get("running"):
        status = "running"
    elif counts.get("queued"):
        status = "queued"
    elif any(counts.get(k) for k in ("waiting_login", "waiting_verification", "waiting_account", "blocked_configuration")):
        status = "waiting_action"
    elif counts.get("succeeded") == len(tasks) and tasks:
        status = "completed"
    elif counts.get("failed") == len(tasks) and tasks:
        status = "failed"
    elif counts.get("cancelled") == len(tasks) and tasks:
        status = "cancelled"
    else:
        status = "completed_with_issues"
    return status, counts


def create_analysis(db: Session, company_id: uuid.UUID):
    company = db.get(Company, company_id)
    if not company or company.archived:
        raise HTTPException(404, "公司不存在或已归档")
    if not db.scalar(select(Job.id).where(Job.company_id == company_id).limit(1)):
        raise HTTPException(409, "请先采集这家公司的岗位，再生成分析")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": "analysis:" + str(company_id)})
    existing = db.scalar(select(TaskRun).where(
        TaskRun.company_id == company_id, TaskRun.kind == "company_analysis",
        TaskRun.status.in_(["queued", "running"]),
    ))
    if existing:
        db.commit()
        return existing, False
    task = TaskRun(company_id=company_id, kind="company_analysis",
                   scope_hash="company-analysis",
                   config_snapshot={"company_name": company.name, "days": 30})
    db.add(task)
    db.flush()
    db.add(CompanyRun(task_run_id=task.id, company_id=company_id, scope_hash=task.scope_hash))
    db.add(TaskEvent(task_id=task.id, message="已创建公司分析任务；将使用一次受预算限制的模型请求"))
    db.commit()
    return task, True
