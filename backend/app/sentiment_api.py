"""Authenticated reputation API. Collection and analysis remain explicit operations."""
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.collection import CollectionError
from app.config import get_settings
from app.models import AuditEvent, Report, SourceAccount, Subject, TaskEvent, TaskRun, now
from app.security import CurrentUser, Db
from app.sentiment_models import SentimentItem, SentimentObservation, SentimentSource, SentimentSourceRun
from app.sentiment_service import (
    PLATFORMS, SAMPLE_LIMITATION, SentimentDocument, create_sentiment_task,
    ingest_documents, selectable_item_url, validate_source_url,
)
from app.serialization import row

router = APIRouter(prefix="/api/v1/sentiment", tags=["sentiment"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubjectInput(Input):
    name: str = Field(min_length=1, max_length=250)
    type: Literal["own_company", "brand", "product"] = "own_company"
    aliases: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    exclusions: list[str] = Field(default_factory=list, max_length=50)
    active: bool = True

    @field_validator("name")
    @classmethod
    def name_valid(cls, value):
        if not value.strip():
            raise ValueError("名称不能为空白")
        return value.strip()

    @field_validator("aliases", "keywords", "exclusions")
    @classmethod
    def words_valid(cls, value):
        if any(not word.strip() or len(word) > 150 for word in value):
            raise ValueError("关键词不能为空，单个关键词最多150字")
        return list(dict.fromkeys(word.strip() for word in value))


class SubjectPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=250)
    aliases: list[str] | None = Field(default=None, max_length=30)
    keywords: list[str] | None = Field(default=None, max_length=50)
    exclusions: list[str] | None = Field(default=None, max_length=50)
    active: bool | None = None


class SourceInput(Input):
    subject_id: uuid.UUID
    kind: str = "public_web"
    name: str = Field(min_length=1, max_length=160)
    entry_urls: list[str] = Field(default_factory=list, max_length=20)
    account_id: uuid.UUID | None = None
    enabled: bool = True
    attribution_basis: Literal["entity_match", "user_selected_url"] = "entity_match"

    @model_validator(mode="after")
    def validate_source(self):
        if self.kind not in PLATFORMS:
            raise ValueError("舆情来源未登记")
        if self.kind in {"public_web", "news"} and not self.entry_urls:
            raise ValueError("公开网页来源需要至少一个网址")
        for url in self.entry_urls:
            validate_source_url(url, self.kind)
        if self.attribution_basis == "user_selected_url" and (not self.entry_urls or
                any(not selectable_item_url(url, self.kind) for url in self.entry_urls)):
            raise ValueError("人工归属只接受明确选定的单篇原文链接，不能用于首页、搜索或栏目")
        self.entry_urls = list(dict.fromkeys(self.entry_urls))
        return self


class SourcePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    entry_urls: list[str] | None = Field(default=None, max_length=20)
    account_id: uuid.UUID | None = None
    enabled: bool | None = None
    attribution_basis: Literal["entity_match", "user_selected_url"] | None = None


class RunInput(Input):
    subject_id: uuid.UUID
    mode: Literal["collect", "analyze", "bettafish"] = "collect"
    source_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    time_from: datetime | None = None
    time_to: datetime | None = None
    analysis_topic: str = Field(default="整体声誉", min_length=1, max_length=300)
    max_pages: int = Field(default=2, ge=1, le=5)
    render: bool | None = None

    @model_validator(mode="after")
    def dates_valid(self):
        for value in (self.time_from, self.time_to):
            if value and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("时间范围必须带时区")
        if self.time_from and self.time_to and self.time_from > self.time_to:
            raise ValueError("开始时间不能晚于结束时间")
        return self


class ImportInput(Input):
    subject_id: uuid.UUID
    items: list[SentimentDocument] = Field(min_length=1, max_length=100)
    note: str = Field(default="人工导入", max_length=500)


class ItemPatch(Input):
    is_read: bool | None = None
    is_favorite: bool | None = None
    is_ignored: bool | None = None
    notes: str | None = Field(default=None, max_length=5000)


def require(db, model, identifier):
    obj = db.get(model, identifier)
    if obj is None:
        raise HTTPException(404, "记录不存在")
    return obj


def check_account(db, identifier, kind):
    if identifier:
        account = require(db, SourceAccount, identifier)
        if account.kind != kind:
            raise HTTPException(422, "账号与舆情平台不匹配")


@router.get("/status")
def module_status(db: Db, user: CurrentUser):
    from app.bettafish_bridge import BettaFishBridge
    return {"status": "available", "collection": "bounded_public_pages_and_import",
            "analysis": "explicit_bounded_model_call", "bettafish": BettaFishBridge().health(),
            "page_budget_scope": "entries_details_and_observed_next_pages",
            "browser_default_platforms": ["wb", "xhs", "dy"],
            "render_modes": {"null": "平台自动选择", "true": "使用浏览器", "false": "匿名来源仅HTTP；绑定账号仍使用会话浏览器"},
            "platforms": sorted(PLATFORMS), "limitations": [SAMPLE_LIMITATION,
                "各社交平台需逐条页面验证可读性；登录可通过数据源中心完成，不保证未登录搜索可用。"]}


@router.get("/subjects")
def list_subjects(db: Db, user: CurrentUser):
    return [row(subject) for subject in db.scalars(select(Subject).where(
        Subject.type.in_(["own_company", "brand", "product"])).order_by(Subject.created_at.desc()))]


@router.post("/subjects", status_code=201)
def add_subject(data: SubjectInput, db: Db, user: CurrentUser):
    subject = Subject(**data.model_dump())
    db.add(subject)
    db.flush()
    db.add(AuditEvent(user_id=user.id, action="sentiment.subject.created", target=str(subject.id)))
    db.commit()
    return row(subject)


@router.patch("/subjects/{subject_id}")
def patch_subject(subject_id: uuid.UUID, data: SubjectPatch, db: Db, user: CurrentUser):
    subject = require(db, Subject, subject_id)
    if subject.type not in {"own_company", "brand", "product"}:
        raise HTTPException(422, "请在对应模块修改此关注对象")
    fields = data.model_dump(exclude_unset=True)
    if any(value is None for value in fields.values()):
        raise HTTPException(422, "字段不能为null；清空关键词请使用空数组")
    try:
        candidate = SubjectInput.model_validate({key: fields.get(key, getattr(subject, key))
                                                for key in SubjectInput.model_fields})
    except ValueError:
        raise HTTPException(422, "关注对象名称或关键词格式无效") from None
    for key, value in candidate.model_dump().items():
        setattr(subject, key, value)
    db.add(AuditEvent(user_id=user.id, action="sentiment.subject.updated", target=str(subject.id)))
    db.commit()
    return row(subject)


@router.get("/sources")
def list_sources(db: Db, user: CurrentUser, subject_id: uuid.UUID | None = None):
    query = select(SentimentSource)
    if subject_id:
        query = query.where(SentimentSource.subject_id == subject_id)
    results = []
    for source in db.scalars(query.order_by(SentimentSource.created_at.desc())):
        latest = db.scalar(select(SentimentSourceRun).where(SentimentSourceRun.source_id == source.id)
                           .order_by(SentimentSourceRun.created_at.desc()).limit(1))
        account = db.get(SourceAccount, source.account_id) if source.account_id else None
        results.append({**row(source), "attribution_basis": source.config.get("attribution_basis", "entity_match"),
                        "latest_run": row(latest) if latest else None,
                        "account_health": account.health if account else None})
    return results


@router.post("/sources", status_code=201)
def add_source(data: SourceInput, db: Db, user: CurrentUser):
    require(db, Subject, data.subject_id)
    check_account(db, data.account_id, data.kind)
    values = data.model_dump()
    attribution_basis = values.pop("attribution_basis")
    source = SentimentSource(**values, config={"attribution_basis": attribution_basis})
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该对象已存在同名来源") from None
    return {**row(source), "attribution_basis": attribution_basis}


@router.patch("/sources/{source_id}")
def patch_source(source_id: uuid.UUID, data: SourcePatch, db: Db, user: CurrentUser):
    source = require(db, SentimentSource, source_id)
    fields = data.model_dump(exclude_unset=True)
    if any(value is None and key != "account_id" for key, value in fields.items()):
        raise HTTPException(422, "来源字段不能为空")
    try:
        candidate = SourceInput.model_validate({key: fields.get(key, source.config.get("attribution_basis", "entity_match")
                                                if key == "attribution_basis" else getattr(source, key))
                                               for key in SourceInput.model_fields})
    except ValueError:
        raise HTTPException(422, "来源名称、网址或平台格式无效") from None
    check_account(db, candidate.account_id, candidate.kind)
    for key, value in candidate.model_dump().items():
        if key == "attribution_basis":
            source.config = {**source.config, "attribution_basis": value}
        else:
            setattr(source, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该对象已存在同名来源") from None
    return {**row(source), "attribution_basis": source.config.get("attribution_basis", "entity_match")}


@router.post("/runs", status_code=202)
def create_run(data: RunInput, db: Db, user: CurrentUser):
    try:
        task = create_sentiment_task(db, **data.model_dump())
    except CollectionError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message}) from None
    return {"task_id": str(task.id), "status": task.status}


@router.get("/runs")
def list_runs(db: Db, user: CurrentUser, subject_id: uuid.UUID | None = None,
              limit: int = Query(50, ge=1, le=100)):
    query = select(TaskRun).where(TaskRun.domain == "sentiment")
    if subject_id:
        query = query.where(TaskRun.config_snapshot["subject"]["id"].astext == str(subject_id))
    return [row(task) for task in db.scalars(query.order_by(TaskRun.created_at.desc()).limit(limit))]


@router.get("/runs/{task_id}")
def get_run(task_id: uuid.UUID, db: Db, user: CurrentUser):
    task = require(db, TaskRun, task_id)
    if task.domain != "sentiment":
        raise HTTPException(404, "舆情任务不存在")
    sources = db.scalars(select(SentimentSourceRun).where(SentimentSourceRun.task_id == task_id)
                        .order_by(SentimentSourceRun.created_at)).all()
    return {**row(task), "sources": [row(source) for source in sources],
            "events": [row(event) for event in db.scalars(select(TaskEvent).where(
                TaskEvent.task_id == task_id).order_by(TaskEvent.created_at).limit(300))]}


@router.post("/runs/{task_id}/retry", status_code=202)
def retry_run(task_id: uuid.UUID, db: Db, user: CurrentUser):
    old = require(db, TaskRun, task_id)
    if old.domain != "sentiment":
        raise HTTPException(404, "舆情任务不存在")
    if old.status in {"queued", "running"}:
        raise HTTPException(409, "任务仍在执行")
    if old.kind == "sentiment_import":
        raise HTTPException(422, "导入任务请重新提交原始数据")
    from app.sentiment_service import parse_date
    snapshot = old.config_snapshot
    mode = {"sentiment_collection": "collect", "sentiment_analysis": "analyze", "sentiment_bettafish": "bettafish"}.get(old.kind)
    if not mode:
        raise HTTPException(422, "不支持重试该舆情任务类型")
    try:
        task = create_sentiment_task(db, uuid.UUID(snapshot["subject"]["id"]), mode=mode,
            source_ids=[uuid.UUID(source["id"]) for source in snapshot.get("sources", [])],
            time_from=parse_date(snapshot.get("time_from")), time_to=parse_date(snapshot.get("time_to")),
            analysis_topic=snapshot.get("analysis_topic", "整体声誉"), max_pages=snapshot.get("max_pages", 2),
            render=snapshot.get("render", False))
    except CollectionError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message}) from None
    return {"task_id": str(task.id), "previous_task_id": str(old.id), "status": task.status}


@router.post("/imports", status_code=201)
def import_items(data: ImportInput, db: Db, user: CurrentUser):
    subject = require(db, Subject, data.subject_id)
    if not subject.active:
        raise HTTPException(422, "关注对象已停用")
    task = TaskRun(domain="sentiment", kind="sentiment_import", status="running", started_at=now(),
                   config_snapshot={"subject": row(subject), "note": data.note})
    db.add(task)
    db.flush()
    groups = defaultdict(list)
    for document in data.items:
        groups[document.platform].append(document)
    all_counts = []
    for platform, documents in groups.items():
        source_run = SentimentSourceRun(task_id=task.id, subject_id=subject.id, platform=platform,
                                        status="succeeded", coverage="user_import",
                                        limitations=["用户提供的样本，未独立验证来源真实性。", SAMPLE_LIMITATION],
                                        config_snapshot={"mode": "import", "user_id": str(user.id)})
        db.add(source_run)
        db.flush()
        counts = ingest_documents(db, task, source_run, row(subject), documents)
        source_run.finished_at = now()
        all_counts.append(counts)
    task.status, task.finished_at, task.progress = "succeeded", now(), 100
    task.summary = {key: sum(count.get(key, 0) for count in all_counts)
                    for key in {k for count in all_counts for k, value in count.items() if isinstance(value, int)}}
    task.summary["pending_attribution"] = [item for count in all_counts for item in count.get("pending_attribution", [])][:20]
    db.add(AuditEvent(user_id=user.id, action="sentiment.imported", target=str(task.id), detail=task.summary))
    db.commit()
    return {"task_id": str(task.id), "counts": task.summary}


@router.get("/items")
def list_items(db: Db, user: CurrentUser, subject_id: uuid.UUID | None = None,
               platform: str | None = None, category: str | None = None,
               is_read: bool | None = None, is_favorite: bool | None = None,
               is_ignored: bool | None = None, q: str | None = Query(None, max_length=200),
               limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)):
    query = select(SentimentItem)
    for key, value in {"subject_id": subject_id, "platform": platform, "category": category,
                       "is_read": is_read, "is_favorite": is_favorite, "is_ignored": is_ignored}.items():
        if value is not None:
            query = query.where(getattr(SentimentItem, key) == value)
    if q:
        query = query.where(SentimentItem.content.contains(q, autoescape=True)
                            | SentimentItem.title.contains(q, autoescape=True))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(SentimentItem.last_seen_at.desc()).offset(offset).limit(limit))
    return {"items": [row(item) for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/items/{item_id}")
def get_item(item_id: uuid.UUID, db: Db, user: CurrentUser):
    item = require(db, SentimentItem, item_id)
    observations = db.scalars(select(SentimentObservation).where(SentimentObservation.item_id == item_id)
                              .order_by(SentimentObservation.created_at.desc()).limit(50)).all()
    return {**row(item), "observations": [row(observation) for observation in observations]}


@router.patch("/items/{item_id}")
def patch_item(item_id: uuid.UUID, data: ItemPatch, db: Db, user: CurrentUser):
    item = require(db, SentimentItem, item_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, "字段不能为null")
        setattr(item, key, value)
    db.commit()
    return row(item)


@router.get("/reports")
def list_reports(db: Db, user: CurrentUser, subject_id: uuid.UUID | None = None,
                 limit: int = Query(30, ge=1, le=100)):
    query = select(Report).where(Report.domain == "sentiment")
    if subject_id:
        query = query.where(Report.coverage["subject_id"].astext == str(subject_id))
    return [row(report) for report in db.scalars(query.order_by(Report.created_at.desc()).limit(limit))]


@router.get("/reports/{report_id}")
def get_report(report_id: uuid.UUID, db: Db, user: CurrentUser):
    report = require(db, Report, report_id)
    if report.domain != "sentiment":
        raise HTTPException(404, "舆情报告不存在")
    return row(report)


@router.get("/reports/{report_id}/artifact")
def report_artifact(report_id: uuid.UUID, db: Db, user: CurrentUser):
    report = require(db, Report, report_id)
    if report.domain != "sentiment" or report.coverage.get("engine") != "bettafish":
        raise HTTPException(404, "该报告没有HTML产物")
    if report.cleared_at is not None:
        raise HTTPException(410, "报告内容已按保留策略清理，原始哈希与清理记录仍保留")
    path = get_settings().storage_dir / "bettafish" / str(report.task_id) / "result.html"
    if not path.is_file() or path.is_symlink():
        raise HTTPException(404, "报告文件不存在")
    return FileResponse(path, media_type="text/html", filename="sentiment-report.html", headers={
        "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})
