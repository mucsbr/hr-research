"""Versioned browser-only protocol. No shell, arbitrary JavaScript or filesystem actions."""

import hashlib
import json
import re
import uuid
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL = "probe_v1"
MAX_MESSAGE_BYTES = 4_000_000
MAX_HTML_BYTES = 3_000_000
OPERATIONS = frozenset(
    {
        "browser.open_login",
        "browser.confirm_login",
        "browser.start_task",
        "browser.navigate",
        "browser.read_page",
        "browser.locate",
        "browser.click",
        "browser.scroll",
        "browser.close_session",
        "device.status",
    }
)


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Enrollment(ProbeInput):
    code: str = Field(min_length=20, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    platform: Literal["win32", "darwin"]
    architecture: Literal["x64", "arm64"]
    version: str = Field(min_length=1, max_length=40, pattern=r"^[0-9][a-zA-Z0-9.+_-]*$")
    protocol_version: Literal["probe_v1"] = PROTOCOL
    capabilities: list[
        Literal[
            "browser.open_login",
            "browser.confirm_login",
            "browser.start_task",
            "browser.navigate",
            "browser.read_page",
            "browser.locate",
            "browser.click",
            "browser.scroll",
            "browser.close_session",
            "device.status",
        ]
    ] = Field(min_length=1, max_length=10)


class ProbeHello(ProbeInput):
    protocol_version: Literal["probe_v1"] = PROTOCOL
    paused: bool = False
    chrome_version: str | None = Field(default=None, max_length=100)


class Receipt(ProbeInput):
    command_id: uuid.UUID
    connection_id: uuid.UUID
    status: Literal["succeeded", "failed", "unknown"]
    result: dict = Field(default_factory=dict)
    error_code: str | None = Field(default=None, max_length=80, pattern=r"^[a-z0-9_]+$")

    @model_validator(mode="after")
    def bounded(self):
        if len(json_bytes(self.result)) > MAX_MESSAGE_BYTES:
            raise ValueError("探针回执超过资料上限")
        if self.status != "succeeded" and not self.error_code:
            raise ValueError("失败或未知回执必须包含明确原因")
        return self


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def validate_url(url, roots):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or not parsed.hostname
    ):
        raise ValueError("探针仅打开登记平台的HTTPS页面")
    host = parsed.hostname.casefold().rstrip(".")
    if not any(host == root or host.endswith("." + root) for root in roots):
        raise ValueError("目标页面不属于该来源平台")
    return url


def validate_operation(operation, payload):
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise ValueError("探针操作未登记")
    if len(json_bytes(payload)) > MAX_MESSAGE_BYTES:
        raise ValueError("探针操作超过上限")
    if operation in {"browser.click", "browser.locate"}:
        selector = payload.get("selector")
        if not isinstance(selector, str) or not 1 <= len(selector) <= 500 or "\x00" in selector:
            raise ValueError("点击选择器无效")
    if operation == "browser.scroll" and payload.get("direction") not in {"down", "up"}:
        raise ValueError("滚动方向无效")
    for key in ("session_id", "profile_id", "page_id"):
        if key in payload:
            uuid.UUID(str(payload[key]))
    pattern = payload.get("response_path_pattern")
    if pattern is not None:
        if not isinstance(pattern, str) or len(pattern) > 300:
            raise ValueError("响应路径规则无效")
        re.compile(pattern)
    return payload
