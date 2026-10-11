"""Administrator-owned, encrypted model configuration shared by API and workers."""

from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException
from pydantic import Field, SecretStr, field_validator
from sqlalchemy import Integer, String, Text, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.browser_sessions import cipher
from app.config import get_settings
from app.db import Base, session_factory
from app.models import AuditEvent, Entity
from app.schemas import Input
from app.security import AdminUser, Db

router = APIRouter(prefix="/api/v1/settings")


class ModelConfiguration(Entity, Base):
    __tablename__ = "model_configurations"
    name: Mapped[str] = mapped_column(String(40), unique=True, default="default")
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class ModelConfigurationError(Exception):
    pass


def normalize_base_url(value):
    value = value.strip().rstrip("/")
    if not value:
        return ""
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("Base URL格式无效") from None
    if (url.scheme not in {"http", "https"} or not url.hostname or url.username
            or url.password or url.query or url.fragment
            or any(ord(c) < 33 for c in value) or port == 0):
        raise ValueError("请输入不含账号、密码、查询参数的HTTP/HTTPS Base URL")
    path = url.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


class ModelConfigurationInput(Input):
    base_url: str = Field(max_length=2000)
    model: str = Field(min_length=1, max_length=300)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    fallback_model: str = Field(default="", max_length=300)
    enabled: bool = True
    daily_request_limit: int = Field(ge=0, le=10000)
    task_call_limit: int = Field(ge=0, le=1000)
    max_output_tokens: int = Field(ge=1, le=131072)
    max_input_chars: int = Field(ge=1, le=2000000)
    timeout_seconds: int = Field(ge=1, le=600)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    output_token_parameter: str = "max_tokens"
    expected_revision: int = Field(ge=0)

    @field_validator("base_url")
    @classmethod
    def base(cls, value):
        return normalize_base_url(value)

    @field_validator("model", "fallback_model")
    @classmethod
    def model_name(cls, value):
        if any(ord(c) < 32 for c in value):
            raise ValueError("模型名不能包含控制字符")
        return value

    @field_validator("api_key")
    @classmethod
    def secret(cls, value):
        if value and any(c in value.get_secret_value() for c in ("\r", "\n")):
            raise ValueError("API Key不能包含换行")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def reasoning(cls, value):
        if value not in {None, "", "none", "minimal", "low", "medium", "high", "max"}:
            raise ValueError("推理参数无效")
        return value or None

    @field_validator("output_token_parameter")
    @classmethod
    def token_parameter(cls, value):
        if value not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("输出预算参数无效")
        return value


def effective_configuration(db, settings=None):
    settings = settings or get_settings()
    record = db.scalar(select(ModelConfiguration).where(ModelConfiguration.name == "default"))
    if not record:
        return settings, "deployment", 0
    try:
        key = cipher().decrypt(record.encrypted_api_key.encode()).decode() if record.encrypted_api_key else ""
    except (InvalidToken, ValueError):
        raise ModelConfigurationError("模型密钥无法解密，请由管理员重新保存") from None
    updates = {"llm_" + name: value for name, value in record.parameters.items() if name != "enabled"}
    updates["llm_api_key"] = SecretStr(key if record.parameters.get("enabled", True) else "")
    return settings.model_copy(update=updates), "platform", record.revision


def get_model_settings(settings=None):
    with session_factory()() as db:
        return effective_configuration(db, settings)[0]


def public_configuration(db):
    settings, origin, revision = effective_configuration(db)
    record = db.scalar(select(ModelConfiguration).where(ModelConfiguration.name == "default"))
    has_key = bool(record.encrypted_api_key) if record else bool(settings.llm_api_key.get_secret_value())
    return {
        "base_url": settings.llm_base_url, "model": settings.llm_model,
        "fallback_model": settings.llm_fallback_model,
        "enabled": record.parameters.get("enabled", True) if record else bool(settings.llm_base_url and has_key and settings.llm_model),
        "has_api_key": has_key, "configured": bool(settings.llm_base_url and settings.llm_model and settings.llm_api_key.get_secret_value()),
        "daily_request_limit": settings.llm_daily_request_limit,
        "task_call_limit": settings.llm_task_call_limit,
        "max_output_tokens": settings.llm_max_output_tokens,
        "max_input_chars": settings.llm_max_input_chars,
        "timeout_seconds": settings.llm_timeout_seconds,
        "reasoning_effort": settings.llm_reasoning_effort,
        "output_token_parameter": settings.llm_output_token_parameter,
        "origin": origin, "revision": revision, "protocol": "chat_completions",
    }


@router.get("/models")
def model_configuration(db: Db, user: AdminUser):
    try:
        return public_configuration(db)
    except ModelConfigurationError as error:
        raise HTTPException(503, str(error)) from None


@router.put("/models")
def save_model_configuration(data: ModelConfigurationInput, db: Db, user: AdminUser):
    # Serializes absent-row creation, updates and the existing global request budget.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "llm-global-budget"})
    record = db.scalar(select(ModelConfiguration).where(ModelConfiguration.name == "default").with_for_update())
    revision = record.revision if record else 0
    if data.expected_revision != revision:
        raise HTTPException(409, "模型配置已被修改，请刷新后重试")
    previous_base = record.parameters.get("base_url", "") if record else get_settings().llm_base_url
    if data.clear_api_key and data.api_key and data.api_key.get_secret_value():
        raise HTTPException(422, "清除密钥和填写新密钥不能同时选择")
    supplied = data.api_key.get_secret_value().strip() if data.api_key else ""
    if supplied:
        key = supplied
    elif data.clear_api_key:
        key = ""
    else:
        try:
            key = cipher().decrypt(record.encrypted_api_key.encode()).decode() if record and record.encrypted_api_key else (
                "" if record else get_settings().llm_api_key.get_secret_value())
        except (InvalidToken, ValueError):
            raise HTTPException(422, "原密钥无法解密，请填写新API Key") from None
        if key and data.base_url != normalize_base_url(previous_base):
            raise HTTPException(422, "更换Base URL时请填写该服务的API Key，或先清除原密钥")
    if data.enabled and (not key or not data.base_url):
        raise HTTPException(422, "启用模型需要Base URL、API Key和模型名")
    parameters = data.model_dump(exclude={"api_key", "clear_api_key", "expected_revision"})
    if not record:
        record = ModelConfiguration(name="default")
        db.add(record)
    record.parameters = parameters
    record.encrypted_api_key = cipher().encrypt(key.encode()).decode() if key else None
    record.revision = revision + 1
    db.add(AuditEvent(user_id=user.id, action="model.configuration_updated", target="default",
                      detail={"revision": record.revision, "enabled": data.enabled,
                              "model": data.model, "key_changed": bool(supplied) or data.clear_api_key}))
    db.commit()
    return public_configuration(db)
