import uuid
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginInput(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=500)


class CompanyInput(Input):
    name: str = Field(min_length=1, max_length=250)
    official_name: str | None = Field(default=None, max_length=250)
    role: Literal["competitor", "own", "other"] = "competitor"
    website: str | None = None
    aliases: list[str] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=4000)

    @field_validator("website")
    @classmethod
    def public_website(cls, value):
        if not value:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("官网需为不含凭据的 HTTP/HTTPS 网址")
        return value


class CompanyPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=250)
    official_name: str | None = Field(default=None, max_length=250)
    role: Literal["competitor", "own", "other"] | None = None
    website: str | None = None
    aliases: list[str] | None = Field(default=None, max_length=30)
    notes: str | None = Field(default=None, max_length=4000)
    archived: bool | None = None

    _public_website = field_validator("website")(CompanyInput.public_website.__func__)


class SourceInput(Input):
    company_id: uuid.UUID
    kind: Literal["careers", "boss", "liepin", "zhaopin", "51job"]
    entry_url: str = Field(max_length=2000)
    adapter: Literal["generic", "tencent"] = "generic"
    account_id: uuid.UUID | None = None
    external_company_id: str | None = Field(default=None, max_length=200)
    config: dict = Field(default_factory=dict)

    @field_validator("entry_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入完整的 http/https 招聘网址")
        if parsed.username or parsed.password:
            raise ValueError("网址不能包含账号密码")
        return value


class ScopeFilters(Input):
    cities: list[str] = Field(default_factory=list, max_length=50)
    keywords: list[str] = Field(default_factory=list, max_length=50)
    exclusions: list[str] = Field(default_factory=list, max_length=50)
    sources: list[Literal["careers", "boss", "liepin", "zhaopin", "51job"]] = Field(default_factory=list)
    max_pages: int = Field(default=10, ge=1, le=100)
    max_details: int = Field(default=50, ge=1, le=500)


class RunInput(Input):
    company_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    name: str = Field(default="招聘信息更新", max_length=200)
    filters: ScopeFilters = Field(default_factory=ScopeFilters)
    idempotency_key: str | None = Field(default=None, max_length=200)


class PlanInput(Input):
    name: str = Field(min_length=1, max_length=200)
    company_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)
    filters: ScopeFilters = Field(default_factory=ScopeFilters)
    schedule: str | None = Field(default=None, max_length=100)
    enabled: bool = False
    timezone: str = "Asia/Shanghai"


class AccountInput(Input):
    kind: Literal["boss", "liepin", "zhaopin", "51job", "xhs", "dy", "wb"]
    name: str = Field(min_length=1, max_length=120)
