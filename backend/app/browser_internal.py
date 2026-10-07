import hmac
import uuid

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field
import redis
from sqlalchemy import select

from app.collection import CollectionError
from app.config import get_settings
from app.db import session_factory
from app.execution import active_execution
from app.models import SourceAccount, SourceBinding, SourceRun, TaskEvent, TaskRun, now
from app.platform_collectors import PUBLIC_PLATFORMS, collect_platform
from app.schemas import Input, ScopeFilters

router = APIRouter(prefix="/api/internal")


class CollectRequest(Input):
    task_id: uuid.UUID
    binding_id: uuid.UUID
    filters: ScopeFilters
    expected_attempt: int = Field(ge=1)


def progress(task_id, message, expected_attempt):
    with session_factory()() as db:
        task = active_execution(db, task_id, expected_attempt)
        if not task or task.status != "running" or task.cancel_requested:
            raise CollectionError("cancelled", "采集任务已停止")
        if task.started_at and (now() - task.started_at).total_seconds() > get_settings().task_timeout_seconds:
            raise CollectionError("time_budget", "任务已达到时间预算")
        task.heartbeat_at = now()
        db.add(TaskEvent(task_id=task.id, message=message))
        db.commit()


@router.post("/browser/collect")
def collect(data: CollectRequest, x_hr_worker_token: str | None = Header(default=None)):
    secret = get_settings().app_secret.get_secret_value()
    if not secret or not x_hr_worker_token or not hmac.compare_digest(secret, x_hr_worker_token):
        raise HTTPException(403, "工作节点认证失败")
    with session_factory()() as db:
        task = db.get(TaskRun, data.task_id)
        binding = db.get(SourceBinding, data.binding_id)
        if not task or not binding or task.company_id != binding.company_id:
            raise HTTPException(404, "采集目标不存在")
        if task.status != "running" or task.cancel_requested:
            raise HTTPException(409, "任务未处于运行状态")
        if task.attempt != data.expected_attempt:
            return JSONResponse({"error_code": "stale_execution", "detail": "旧执行不能继续浏览器采集"}, status_code=409)
        frozen = next((source for source in task.config_snapshot.get("sources_snapshot", [])
                       if source["id"] == str(binding.id)), None)
        if not frozen or frozen.get("account_id") != (str(binding.account_id) if binding.account_id else None):
            raise HTTPException(409, "来源账号配置已变化，请创建新任务")
        if binding.kind not in {"boss", "liepin", "zhaopin", "51job"} or frozen["kind"] != binding.kind:
            raise HTTPException(422, "不支持的浏览器采集来源")
        account = db.get(SourceAccount, binding.account_id) if binding.account_id else None
        if (account and (account.kind != binding.kind or account.health not in {"saved", "ready"})) or (
            not account and binding.kind not in PUBLIC_PLATFORMS
        ):
            return JSONResponse({"error_code": "waiting_login", "detail": "请先保存有效登录会话"}, status_code=409)
        identity = task.config_snapshot.get("company_identity") or {
            "name": task.config_snapshot.get("company_name"), "aliases": []
        }
        account_id, kind, binding_data = account.id if account else None, binding.kind, frozen
        source_id = db.scalar(select(SourceRun.id).where(SourceRun.task_id == task.id, SourceRun.binding_id == binding.id))
    lease = redis.Redis.from_url(get_settings().redis_url.get_secret_value()).lock(
        "account:" + str(account_id) if account_id else "anonymous-binding:" + str(data.binding_id),
        timeout=get_settings().task_timeout_seconds + 90,
        blocking=False, thread_local=False,
    )
    if not lease.acquire():
        return JSONResponse({"error_code": "waiting_account", "detail": "账号正被登录或其他采集任务使用"}, status_code=409)
    try:
        progress(data.task_id, f"{kind} 已取得来源执行锁" + ("，使用已保存账号" if account_id else "，尝试匿名读取"), data.expected_attempt)
        if source_id is None:
            raise CollectionError("code_runtime_scope", "来源运行尚未建立，未开始浏览器采集")
        from app.adapter_code_runtime import freeze_for_task
        descriptor = freeze_for_task(kind, task_id=data.task_id, binding_id=data.binding_id, expected_attempt=data.expected_attempt)
        result = collect_platform(account_id, kind, binding_data, identity, data.filters.model_dump(),
                                  lambda message: progress(data.task_id, message, data.expected_attempt),
                                  descriptor=descriptor, runtime_scope={"task_id": data.task_id,
                                      "source_run_id": source_id, "expected_attempt": data.expected_attempt})
        progress(data.task_id, f"{kind} 浏览器已完成本执行的采集", data.expected_attempt)
        if account_id:
            with session_factory()() as db:
                active_execution(db, data.task_id, data.expected_attempt)
                account = db.get(SourceAccount, account_id)
                account.health, account.last_verified_at = "ready", now()
                db.commit()
        return {
            "records": [record.model_dump(mode="json") for record in result.records],
            "documents": result.documents, "coverage": result.coverage, "pages": result.pages,
            "stop_reason": result.stop_reason, "errors": result.errors,
            "adapter_fingerprint": result.adapter_fingerprint,
            "code_execution": result.code_execution,
        }
    except CollectionError as exc:
        if account_id and exc.code in {"waiting_login", "waiting_verification"}:
            with session_factory()() as db:
                try:
                    active_execution(db, data.task_id, data.expected_attempt)
                except CollectionError:
                    return JSONResponse({"error_code": "stale_execution", "detail": "旧执行不再更新账号状态"}, status_code=409)
                account = db.get(SourceAccount, account_id)
                if exc.code == "waiting_login":
                    account.health = "expired"
                elif exc.code == "waiting_verification":
                    account.health = "verification_required"
                db.commit()
        return JSONResponse({"error_code": exc.code, "detail": exc.message,
                             "repair_evidence": getattr(exc, "repair_evidence", None),
                             "code_failure_evidence": getattr(exc, "code_failure_evidence", None),
                             "code_execution": getattr(exc, "code_execution", {}),
                             "adapter_fingerprint": getattr(exc, "adapter_fingerprint", "")}, status_code=409)
    except Exception:
        return JSONResponse({"error_code": "browser_collection_failed",
                             "detail": "浏览器采集未完成，请检查来源或登录会话"}, status_code=503)
    finally:
        try:
            lease.release()
        except redis.exceptions.LockError:
            pass
