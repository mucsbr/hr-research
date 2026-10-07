import json
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
import soupsieve

from app.collection import CollectionError, validate_fetch_url
from app.models import AuditEvent, Company, SourceAccount, SourceBinding
from app.recruitment_discovery import bind_candidate, bulk_companies, discover_entries, public_url_syntax
from app.recruitment_management_models import (
    DiscoverySearchConfig, JobMatchProposal, JobMergeRecord, RecruitmentEntryCandidate,
)
from app.recruitment_matching import merge_proposal, propose_matches, undo_merge
from app.recruitment_search import effective_search_configuration, validate_search_endpoint
from app.schemas import CompanyInput, Input
from app.security import AdminUser, CurrentUser, Db
from app.serialization import row
from app.website_agent import WebsitePlan

router = APIRouter(prefix="/api/v1")


def require(db, model, identifier):
    result = db.get(model, identifier)
    if not result:
        raise HTTPException(404, "记录不存在")
    return result


def translate(exc):
    return HTTPException(409, {"code": exc.code, "message": exc.message})


class BulkCompanyInput(Input):
    companies: list[CompanyInput] = Field(default_factory=list, max_length=100)
    names: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def one_mode(self):
        if bool(self.companies) == bool(self.names and self.names.strip()):
            raise ValueError("提供公司列表或逐行公司名中的一种")
        if self.names and len([line for line in self.names.splitlines() if line.strip()]) > 100:
            raise ValueError("单次最多100家公司")
        return self


class DiscoveryInput(Input):
    max_candidates: int = Field(default=10, ge=1, le=20)


class SearchConfiguration(Input):
    endpoint_url: str = Field(max_length=2000)
    enabled: bool = True
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("endpoint_url")
    @classmethod
    def url(cls, value):
        return validate_search_endpoint(value)


class SourcePatch(Input):
    entry_url: str | None = Field(default=None, max_length=2000)
    account_id: uuid.UUID | None = None
    external_company_id: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    config: dict | None = None

    @field_validator("entry_url")
    @classmethod
    def url(cls, value):
        return public_url_syntax(value) if value is not None else None

    @field_validator("config")
    @classmethod
    def safe_config(cls, value):
        if value is None:
            return value
        if len(json.dumps(value, ensure_ascii=False)) > 20000:
            raise ValueError("来源配置超过大小限制")
        def inspect(node):
            if isinstance(node, dict):
                for key, item in node.items():
                    if any(secret in key.casefold() for secret in ("password", "cookie", "token", "secret", "api_key")):
                        raise ValueError("来源配置不能保存登录或接口密钥，请使用账号管理")
                    inspect(item)
            elif isinstance(node, list):
                for item in node:
                    inspect(item)
        inspect(value)
        if "website_plan" in value:
            WebsitePlan.model_validate(value["website_plan"])
        for key in ("job_link_selector", "next_selector", "complete_selector"):
            if value.get(key):
                if not isinstance(value[key], str) or len(value[key]) > 200:
                    raise ValueError("选择器格式或长度不正确")
                try:
                    soupsieve.compile(value[key])
                except Exception as exc:
                    raise ValueError("无效的 CSS 选择器") from exc
        return value


class MergeInput(Input):
    target_job_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/companies/bulk", status_code=201)
def add_companies(data: BulkCompanyInput, db: Db, user: CurrentUser):
    entries = [item.model_dump() for item in data.companies] if data.companies else [
        {"name": name.strip()} for name in data.names.splitlines() if name.strip()]
    try:
        result = bulk_companies(db, entries, user.id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from exc
    return result


@router.get("/settings/recruitment-discovery")
def get_discovery_config(db: Db, user: AdminUser):
    try:
        config = effective_search_configuration(db)
    except CollectionError as exc:
        raise translate(exc) from None
    return {"provider": "searxng", "configured": bool(config), "config": config,
            "help": "填写可访问且支持 format=json 的 SearXNG /search 地址；仅搜索候选，不自动访问或绑定公司站点"}


@router.put("/settings/recruitment-discovery")
def save_discovery_config(data: SearchConfiguration, db: Db, user: AdminUser):
    config = db.scalar(select(DiscoverySearchConfig).where(DiscoverySearchConfig.name == "default"))
    if not config:
        config = DiscoverySearchConfig(name="default", **data.model_dump())
        db.add(config)
    else:
        for key, value in data.model_dump().items():
            setattr(config, key, value)
    db.add(AuditEvent(user_id=user.id, action="recruitment.search_config_updated", target="default",
                      detail={"enabled": data.enabled, "endpoint_url": data.endpoint_url}))
    db.commit()
    return row(config)


@router.post("/companies/{company_id}/entry-discovery")
def discover(company_id: uuid.UUID, data: DiscoveryInput, db: Db, user: CurrentUser):
    company = require(db, Company, company_id)
    try:
        result = discover_entries(db, company, user.id, data.max_candidates)
        db.commit()
        return result
    except CollectionError as exc:
        db.rollback()
        db.add(AuditEvent(user_id=user.id, action="recruitment.discovery_failed", target=str(company_id),
                          detail={"code": exc.code, "search": getattr(exc, "search_evidence", {})}))
        db.commit()
        raise translate(exc) from None


@router.get("/companies/{company_id}/entry-candidates")
def entry_candidates(company_id: uuid.UUID, db: Db, user: CurrentUser):
    require(db, Company, company_id)
    return [row(item) for item in db.scalars(select(RecruitmentEntryCandidate).where(
        RecruitmentEntryCandidate.company_id == company_id).order_by(RecruitmentEntryCandidate.created_at.desc()))]


@router.post("/entry-candidates/{candidate_id}/bind")
def bind_entry(candidate_id: uuid.UUID, db: Db, user: CurrentUser):
    candidate = require(db, RecruitmentEntryCandidate, candidate_id)
    try:
        source = bind_candidate(db, candidate, user.id)
        db.commit()
        return {"candidate": row(candidate), "source": row(source)}
    except CollectionError as exc:
        db.rollback()
        raise translate(exc) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "来源已被另一个操作绑定，请刷新") from None


@router.post("/entry-candidates/{candidate_id}/reject")
def reject_entry(candidate_id: uuid.UUID, db: Db, user: CurrentUser):
    candidate = require(db, RecruitmentEntryCandidate, candidate_id)
    if candidate.status == "bound":
        raise HTTPException(409, "已绑定候选不能直接拒绝；可停用对应来源")
    candidate.status = "rejected"
    db.add(AuditEvent(user_id=user.id, action="recruitment.entry_rejected", target=str(candidate.id)))
    db.commit()
    return row(candidate)


@router.patch("/source-bindings/{binding_id}")
def patch_source(binding_id: uuid.UUID, data: SourcePatch, db: Db, user: CurrentUser):
    binding = require(db, SourceBinding, binding_id)
    values = data.model_dump(exclude_unset=True)
    if any(key in values and values[key] is None for key in ("entry_url", "enabled", "config")):
        raise HTTPException(422, "网址、启用状态和配置不能为 null")
    if "account_id" in values and values["account_id"] is not None:
        account = require(db, SourceAccount, values["account_id"])
        if account.kind != binding.kind:
            raise HTTPException(422, "账号平台与来源不匹配")
    if "entry_url" in values:
        try:
            validate_fetch_url(values["entry_url"])
        except CollectionError as exc:
            raise translate(exc) from None
        from app.platform_collectors import PLATFORM_ROOTS
        from urllib.parse import urlparse
        host = urlparse(values["entry_url"]).hostname or ""
        if binding.kind in PLATFORM_ROOTS:
            root = PLATFORM_ROOTS[binding.kind]
            if host != root and not host.endswith("." + root):
                raise HTTPException(422, "网址域名与招聘平台不匹配")
        if binding.adapter == "tencent" and host != "careers.tencent.com":
            raise HTTPException(422, "腾讯适配器只能使用腾讯招聘官网")
    before = {key: row(binding).get(key) for key in values}
    for key, value in values.items():
        setattr(binding, key, value)
    if values.keys() & {"entry_url", "account_id", "external_company_id", "config"}:
        binding.health = "unknown"
    db.add(AuditEvent(user_id=user.id, action="recruitment.source_updated", target=str(binding.id),
                      detail={"before": before, "after": {key: row(binding).get(key) for key in values},
                              "task_policy": "更改只作用于未来任务；已有任务继续使用冻结配置"}))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "这家公司已有相同来源网址") from None
    return row(binding)


@router.post("/companies/{company_id}/job-matches")
def find_matches(company_id: uuid.UUID, db: Db, user: CurrentUser):
    try:
        result = propose_matches(db, company_id, user.id)
        db.commit()
        return result
    except CollectionError as exc:
        db.rollback()
        raise translate(exc) from None


@router.get("/companies/{company_id}/job-matches")
def list_matches(company_id: uuid.UUID, db: Db, user: CurrentUser, limit: int = Query(100, ge=1, le=200)):
    require(db, Company, company_id)
    return [row(item) for item in db.scalars(select(JobMatchProposal).where(JobMatchProposal.company_id == company_id)
                                            .order_by(JobMatchProposal.created_at.desc()).limit(limit))]


@router.post("/job-matches/{proposal_id}/merge")
def merge_jobs(proposal_id: uuid.UUID, data: MergeInput, db: Db, user: CurrentUser):
    try:
        result = merge_proposal(db, proposal_id, data.target_job_id, user.id, data.reason)
        db.commit()
        return row(result)
    except CollectionError as exc:
        db.rollback()
        raise translate(exc) from None


@router.get("/companies/{company_id}/job-merges")
def list_merges(company_id: uuid.UUID, db: Db, user: CurrentUser):
    require(db, Company, company_id)
    return [row(item) for item in db.scalars(select(JobMergeRecord).where(JobMergeRecord.company_id == company_id)
                                            .order_by(JobMergeRecord.created_at.desc()).limit(100))]


@router.post("/job-merges/{merge_id}/undo")
def unmerge_jobs(merge_id: uuid.UUID, db: Db, user: CurrentUser):
    try:
        result = undo_merge(db, merge_id, user.id)
        db.commit()
        return row(result)
    except CollectionError as exc:
        db.rollback()
        raise translate(exc) from None
