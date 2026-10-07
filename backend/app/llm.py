"""A deliberately small Chat Completions gateway with durable, conservative budgets."""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db import session_factory
from app.models import ModelCall


class ModelError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


@dataclass
class Completion:
    content: str
    model: str
    call_id: uuid.UUID
    usage: dict
    finish_reason: str | None


def shanghai_day_start():
    local = datetime.now(ZoneInfo("Asia/Shanghai"))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def reserve_call(purpose, task_id, input_chars, max_tokens, model):
    settings = get_settings()
    with session_factory()() as db:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                   {"key": "llm-global-budget"})
        count = db.scalar(select(func.count()).select_from(ModelCall).where(
            ModelCall.created_at >= shanghai_day_start()))
        if count >= settings.llm_daily_request_limit:
            raise ModelError("daily_budget", "今日模型请求预算已用完，未发送请求")
        if task_id:
            count = db.scalar(select(func.count()).select_from(ModelCall).where(
                ModelCall.task_id == task_id))
            if count >= settings.llm_task_call_limit:
                raise ModelError("task_budget", "本任务模型调用预算已用完，未发送请求")
        call = ModelCall(task_id=task_id, purpose=purpose, model=model,
                         input_chars=input_chars, max_output_tokens=max_tokens)
        db.add(call)
        db.commit()
        return call.id


def finish_call(call_id, status, usage=None, error=None, finish_reason=None):
    usage = usage or {}
    with session_factory()() as db:
        call = db.get(ModelCall, call_id)
        call.status = status
        call.prompt_tokens = usage.get("prompt_tokens")
        call.completion_tokens = usage.get("completion_tokens")
        call.finish_reason = finish_reason
        call.reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        call.error_code = error
        db.commit()


class ModelGateway:
    def __init__(self, transport=None):
        self.transport = transport

    def complete(self, messages: list[dict[str, str]], *, purpose: str,
                 task_id: uuid.UUID | None = None, stream: bool = False,
                 max_output_tokens: int | None = None, model: str | None = None) -> Completion:
        settings = get_settings()
        if settings.app_env == "test" and self.transport is None:
            raise ModelError("test_network_disabled", "自动化测试禁止连接真实模型服务")
        if not settings.llm_base_url or not settings.llm_api_key.get_secret_value():
            raise ModelError("not_configured", "模型连接未配置")
        if not messages or any(not isinstance(m.get("content"), str) for m in messages):
            raise ModelError("invalid_input", "模型输入必须为文本消息")
        size = sum(len(m["content"]) for m in messages)
        if size > settings.llm_max_input_chars:
            raise ModelError("input_budget", "输入超过本次模型预算，请先缩小资料范围")
        selected = model or settings.llm_model
        if selected not in {settings.llm_model, settings.llm_fallback_model}:
            raise ModelError("model_not_allowed", "模型未登记")
        maximum = min(max_output_tokens or settings.llm_max_output_tokens,
                      settings.llm_max_output_tokens)
        if maximum < 1:
            raise ModelError("invalid_budget", "输出预算必须为正数")
        call_id = reserve_call(purpose, task_id, size, maximum, selected)
        payload = {"model": selected, "messages": messages, "stream": stream,
                   "max_tokens": maximum}
        if selected.startswith(("glm-5.3", "glm-5.2")):
            payload["reasoning_effort"] = settings.llm_reasoning_effort
        if stream:
            payload["stream_options"] = {"include_usage": True}
        usage, content, finish_reason = {}, "", None
        # No automatic retries or silent model fallback: a failed/unknown request may still cost.
        try:
            with httpx.Client(
                timeout=settings.llm_timeout_seconds, trust_env=False,
                transport=self.transport,
                headers={"Authorization": "Bearer " + settings.llm_api_key.get_secret_value()},
            ) as client:
                url = settings.llm_base_url.rstrip("/") + "/chat/completions"
                if stream:
                    with client.stream("POST", url, json=payload) as response:
                        response.raise_for_status()
                        completed = False
                        for line in response.iter_lines():
                            if not line.startswith("data:"):
                                continue
                            body = line[5:].strip()
                            if body == "[DONE]":
                                completed = True
                                break
                            data = json.loads(body)
                            usage = data.get("usage") or usage
                            choices = data.get("choices") or []
                            if choices:
                                content += choices[0].get("delta", {}).get("content") or ""
                                finish_reason = choices[0].get("finish_reason") or finish_reason
                        if not completed and not finish_reason:
                            raise ModelError("stream_interrupted", "模型流式结果中断，未自动重试")
                else:
                    response = client.post(url, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    usage = data.get("usage") or {}
                    choices = data.get("choices") or []
                    if not choices:
                        raise ModelError("invalid_response", "模型未返回可用内容")
                    content = choices[0].get("message", {}).get("content") or ""
                    finish_reason = choices[0].get("finish_reason")
            if finish_reason == "length":
                raise ModelError("output_truncated", "模型输出（含推理）达到预算上限，未自动重试")
            if not content:
                raise ModelError("empty_response", "模型结果为空，未自动重试")
            finish_call(call_id, "completed", usage, finish_reason=finish_reason)
            return Completion(content, selected, call_id, usage, finish_reason)
        except httpx.HTTPStatusError as exc:
            code = "http_" + str(exc.response.status_code)
            finish_call(call_id, "failed", usage, code, finish_reason)
            raise ModelError(code, f"模型服务返回 HTTP {exc.response.status_code}，未自动重试") from None
        except (httpx.RequestError, ValueError, ModelError) as exc:
            code = exc.code if isinstance(exc, ModelError) else type(exc).__name__
            finish_call(call_id, "unknown" if isinstance(exc, httpx.RequestError) else "failed",
                        usage, code, finish_reason)
            if isinstance(exc, ModelError):
                raise
            raise ModelError(code, "模型调用未完成，已记入预算，未自动重试") from None
