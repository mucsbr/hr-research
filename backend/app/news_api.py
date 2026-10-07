"""Authenticated source configuration, manual HR-news tasks, and article curation."""
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.collection import CollectionError
from app.models import AuditEvent, Subject, TaskEvent, TaskRun
from app.news_models import NewsItem, NewsObservation, NewsSource, NewsSourceRun
from app.news_service import create_news_task
from app.security import AdminUser, CurrentUser, Db
from app.sentiment_service import safe_url
from app.serialization import row

router = APIRouter(prefix="/api/v1/news", tags=["news"])


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInput(Input):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["feed", "web"] = "feed"
    url: str
    subject_id: uuid.UUID | None = None
    source_type: Literal["official", "media", "industry", "other"] = "other"
    publisher: str | None = Field(default=None, max_length=200)
    regions: list[str] = Field(default_factory=list, max_length=30)
    topics: list[str] = Field(default_factory=list, max_length=30)
    industries: list[str] = Field(default_factory=list, max_length=30)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    exclusions: list[str] = Field(default_factory=list, max_length=50)
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def validate_url(cls, value):
        return safe_url(value)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError("来源名称不能为空白")
        return value.strip()

    @field_validator("regions", "topics", "industries", "keywords", "exclusions")
    @classmethod
    def validate_tags(cls, value):
        if any(not word.strip() or len(word) > 150 for word in value):
            raise ValueError("标签或关键词不能为空，单项最多150字")
        return list(dict.fromkeys(word.strip() for word in value))


class SourcePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = None
    subject_id: uuid.UUID | None = None
    source_type: Literal["official", "media", "industry", "other"] | None = None
    publisher: str | None = Field(default=None, max_length=200)
    regions: list[str] | None = Field(default=None, max_length=30)
    topics: list[str] | None = Field(default=None, max_length=30)
    industries: list[str] | None = Field(default=None, max_length=30)
    keywords: list[str] | None = Field(default=None, max_length=50)
    exclusions: list[str] | None = Field(default=None, max_length=50)
    enabled: bool | None = None


class RunInput(Input):
    source_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    max_articles: int = Field(default=10, ge=1, le=30)
    max_pages: int = Field(default=3, ge=1, le=5)
    render: bool = False


class ItemPatch(Input):
    is_read: bool | None = None
    is_favorite: bool | None = None
    notes: str | None = Field(default=None, max_length=5000)


def require(db, model, identifier):
    obj = db.get(model, identifier)
    if not obj:
        raise HTTPException(404, "记录不存在")
    return obj


@router.get("/status")
def status(db: Db, user: CurrentUser):
    return {"status": "available", "collection": ["feed", "public_web"], "summary": "source_excerpt",
            "model_calls": "disabled_by_default", "configured_sources": db.scalar(select(func.count()).select_from(NewsSource)),
            "limitations": ["用户配置来源后才开始采集；未预填来源或地区偏好。",
                            "政策生效日期与资讯发布日期分别记录，未知保持空。"]}


@router.get("/sources")
def sources(db: Db, user: CurrentUser):
    results = []
    for source in db.scalars(select(NewsSource).order_by(NewsSource.created_at.desc())):
        latest = db.scalar(select(NewsSourceRun).where(NewsSourceRun.source_id == source.id)
                           .order_by(NewsSourceRun.created_at.desc()).limit(1))
        results.append({**row(source), "latest_run": row(latest) if latest else None})
    return results


@router.post("/sources", status_code=201)
def create_source(data: SourceInput, db: Db, user: AdminUser):
    if data.subject_id:
        require(db, Subject, data.subject_id)
    source = NewsSource(**data.model_dump())
    db.add(source)
    try:
        db.flush()
        db.add(AuditEvent(user_id=user.id, action="news.source.created", target=str(source.id)))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该资讯来源名称已存在") from None
    return row(source)


@router.patch("/sources/{source_id}")
def update_source(source_id: uuid.UUID, data: SourcePatch, db: Db, user: AdminUser):
    source = require(db, NewsSource, source_id)
    values = data.model_dump(exclude_unset=True)
    if any(value is None and key not in {"publisher", "subject_id"} for key, value in values.items()):
        raise HTTPException(422, "来源字段不能为空")
    try:
        validated = SourceInput.model_validate({key: values.get(key, getattr(source, key)) for key in SourceInput.model_fields})
    except ValueError:
        raise HTTPException(422, "来源网址、标签或关键词格式无效") from None
    if validated.subject_id:
        require(db, Subject, validated.subject_id)
    for key, value in validated.model_dump().items():
        setattr(source, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "该资讯来源名称已存在") from None
    return row(source)


@router.post("/runs", status_code=202)
def create_run(data: RunInput, db: Db, user: CurrentUser):
    try:
        task = create_news_task(db, **data.model_dump())
    except CollectionError as exc:
        raise HTTPException(422, {"code": exc.code, "message": exc.message}) from None
    return {"task_id": str(task.id), "status": task.status}


@router.get("/runs")
def list_runs(db: Db, user: CurrentUser, limit: int = Query(50, ge=1, le=100)):
    return [row(task) for task in db.scalars(select(TaskRun).where(TaskRun.domain == "news")
                                            .order_by(TaskRun.created_at.desc()).limit(limit))]


@router.get("/runs/{task_id}")
def get_run(task_id: uuid.UUID, db: Db, user: CurrentUser):
    task = require(db, TaskRun, task_id)
    if task.domain != "news":
        raise HTTPException(404, "资讯任务不存在")
    return {**row(task), "sources": [row(run) for run in db.scalars(select(NewsSourceRun)
            .where(NewsSourceRun.task_id == task.id).order_by(NewsSourceRun.created_at))],
            "events": [row(event) for event in db.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)
                                                          .order_by(TaskEvent.created_at).limit(300))]}


@router.post("/runs/{task_id}/retry", status_code=202)
def retry_run(task_id: uuid.UUID, db: Db, user: CurrentUser):
    old = require(db, TaskRun, task_id)
    if old.domain != "news":
        raise HTTPException(404, "资讯任务不存在")
    if old.status in {"queued", "running"}:
        raise HTTPException(409, "任务仍在执行")
    task = TaskRun(domain="news", kind="news_collection", config_snapshot=old.config_snapshot)
    db.add(task)
    db.commit()
    return {"task_id": str(task.id), "previous_task_id": str(old.id)}


@router.get("/items")
def list_items(db: Db, user: CurrentUser, source_id: uuid.UUID | None = None,
               source_type: str | None = None, content_type: str | None = None,
               region: str | None = None, topic: str | None = None, industry: str | None = None,
               is_read: bool | None = None, is_favorite: bool | None = None,
               q: str | None = Query(None, max_length=200), limit: int = Query(50, ge=1, le=100),
               offset: int = Query(0, ge=0)):
    query = select(NewsItem)
    for key, value in {"source_type": source_type, "content_type": content_type, "is_read": is_read,
                       "is_favorite": is_favorite}.items():
        if value is not None:
            query = query.where(getattr(NewsItem, key) == value)
    for key, value in {"regions": region, "topics": topic, "industries": industry}.items():
        if value:
            query = query.where(getattr(NewsItem, key).contains([value]))
    if source_id:
        query = query.where(NewsItem.id.in_(select(NewsObservation.item_id).join(NewsSourceRun)
                                          .where(NewsSourceRun.source_id == source_id)))
    if q:
        query = query.where(NewsItem.title.contains(q, autoescape=True) | NewsItem.content.contains(q, autoescape=True))
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = db.scalars(query.order_by(NewsItem.last_seen_at.desc()).offset(offset).limit(limit))
    return {"items": [row(item) for item in items], "total": total, "limit": limit, "offset": offset}


@router.get("/items/{item_id}")
def get_item(item_id: uuid.UUID, db: Db, user: CurrentUser):
    item = require(db, NewsItem, item_id)
    observations = db.scalars(select(NewsObservation).where(NewsObservation.item_id == item_id)
                              .order_by(NewsObservation.created_at.desc()).limit(50))
    return {**row(item), "observations": [row(observation) for observation in observations]}


@router.patch("/items/{item_id}")
def patch_item(item_id: uuid.UUID, data: ItemPatch, db: Db, user: CurrentUser):
    item = require(db, NewsItem, item_id)
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is None:
            raise HTTPException(422, "字段不能为null")
        setattr(item, key, value)
    db.commit()
    return row(item)
