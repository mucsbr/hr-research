"""Frozen code releases executed remotely, with durable task-scoped invocation keys."""
from copy import deepcopy
from copy import copy
from dataclasses import dataclass
import time
import uuid

from sqlalchemy import select, text

from app.adapter_code_models import AdapterCodePackage
from app.adapter_code_package import content_sha256, load_bundle
from app.adapter_program import validate_detail_output, validate_list_output, validate_navigation
from app.adapter_runner_client import AdapterRunnerClient
from app.adapter_runtime_models import AdapterProgramExecution
from app.collection import CollectionError
from app.config import get_settings
from app.db import session_factory
from app.execution import active_execution
from app.maintenance_models import AdapterRegistry, AdapterRelease
from app.models import Company, SourceBinding, SourceRun, now


@dataclass(frozen=True)
class FrozenAdapter:
    kind: str
    manifest: dict
    fingerprint: str
    release_id: str | None
    generation: int

    @property
    def program(self):
        return deepcopy(self.manifest.get("program"))


def freeze_active_adapter(kind):
    from app.adapter_maintenance import builtin_manifest, fingerprint, validate_manifest
    with session_factory()() as db:
        registry = db.scalar(select(AdapterRegistry).where(AdapterRegistry.kind == kind).with_for_update())
        if not registry:
            manifest = builtin_manifest(kind)
            if manifest.get("program"):
                raise CollectionError("code_release_unavailable", "代码版本必须经过登记和验证发布")
            return FrozenAdapter(kind, manifest, fingerprint(manifest), None, 0)
        release = db.get(AdapterRelease, registry.active_release_id)
        if not release or release.status != "published" or release.registry_id != registry.id:
            raise CollectionError("adapter_release_unavailable", "活动版本未完成发布")
        manifest = validate_manifest(release.manifest, kind)
        digest = fingerprint(manifest)
        if digest != release.fingerprint:
            raise CollectionError("adapter_release_corrupt", "活动适配器内容与版本摘要不同")
        if manifest.get("program") and (release.validation.get("passed") is not True
                or release.validation.get("fingerprint") != digest
                or release.validation.get("code_published_once") is not True):
            raise CollectionError("code_release_unverified", "代码版本没有匹配的发布验证")
        return FrozenAdapter(kind, deepcopy(manifest), digest, str(release.id), registry.release_generation)


def freeze_for_task(kind, *, task_id, binding_id, expected_attempt):
    """Freeze once per source task, preserving an in-flight version after a publish or takeover."""
    from app.adapter_maintenance import builtin_manifest, fingerprint, validate_manifest
    key = str(binding_id)
    # An already-frozen task must not depend on the health of a later active release.
    with session_factory()() as db:
        task = active_execution(db, uuid.UUID(str(task_id)), expected_attempt)
        snapshot = next((item for item in task.config_snapshot.get("sources_snapshot", []) if item.get("id") == key), None)
        if (not snapshot or snapshot.get("company_id") != str(task.company_id)
                or ("official" if snapshot.get("kind") == "careers" else snapshot.get("kind")) != kind):
            raise CollectionError("code_runtime_scope", "冻结适配器的来源不属于本任务原始范围")
        previous = deepcopy(task.config_snapshot.get("adapter_versions", {}).get(key))
        db.commit()
    # Never hold task and registry locks at the same time.
    candidate = freeze_active_adapter(kind) if previous is None else None
    with session_factory()() as db:
        task = active_execution(db, uuid.UUID(str(task_id)), expected_attempt)
        current_snapshot = next((item for item in task.config_snapshot.get("sources_snapshot", [])
                                 if item.get("id") == key), None)
        if current_snapshot != snapshot:
            raise CollectionError("code_runtime_scope", "来源范围在冻结版本期间变化，未开始读取来源")
        versions = deepcopy(task.config_snapshot.get("adapter_versions", {}))
        previous = versions.get(key)
        if previous is None:
            if candidate is None:
                raise CollectionError("adapter_version_conflict", "已冻结的来源版本被移除，未重新选择活动版本")
            previous = {"kind": kind, "release_id": candidate.release_id,
                        "generation": candidate.generation, "fingerprint": candidate.fingerprint}
            versions[key] = previous
            task.config_snapshot = {**task.config_snapshot, "adapter_versions": versions}
        if previous.get("kind") != kind:
            raise CollectionError("adapter_version_conflict", "本任务已固定其他来源的适配器")
        expected = {"fingerprint": snapshot.get("expected_adapter_fingerprint"),
                    "release_id": snapshot.get("expected_release_id"),
                    "generation": snapshot.get("expected_release_generation")}
        if any(value is not None and previous.get(key) != value for key, value in expected.items()):
            raise CollectionError("adapter_version_conflict", "恢复任务要求的发布版本或代次已变化，未开始读取来源")
        db.commit()
        release = db.get(AdapterRelease, uuid.UUID(previous["release_id"])) if previous["release_id"] else None
        if previous["release_id"] and (not release or release.status not in {"published", "retired"}):
            raise CollectionError("adapter_release_unavailable", "任务固定的历史适配器不可用")
        manifest = validate_manifest(release.manifest, kind) if release else builtin_manifest(kind)
        if fingerprint(manifest) != previous["fingerprint"]:
            raise CollectionError("adapter_release_corrupt", "任务固定的版本内容已变化，未混用新版")
        if release:
            registry = db.get(AdapterRegistry, release.registry_id)
            if not registry or registry.kind != kind:
                raise CollectionError("code_runtime_scope", "冻结版本不属于当前来源平台")
        if manifest.get("program") and (not release or release.validation.get("passed") is not True
                or release.validation.get("fingerprint") != previous["fingerprint"]
                or release.validation.get("code_published_once") is not True):
            raise CollectionError("code_release_unverified", "冻结代码版本缺少原始发布证明")
        return FrozenAdapter(kind, deepcopy(manifest), previous["fingerprint"], previous["release_id"], previous["generation"])


class ProgramRuntime:
    def __init__(self, descriptor, *, task_id, source_run_id, binding_snapshot, expected_attempt,
                 client=None, wait_seconds=40, poll_seconds=0.25, sleep=time.sleep, scope_guard=None):
        from app.adapter_maintenance import fingerprint, validate_manifest
        manifest = validate_manifest(deepcopy(descriptor.manifest), descriptor.kind)
        if fingerprint(manifest) != descriptor.fingerprint:
            raise CollectionError("adapter_release_corrupt", "调用方提供的冻结描述与内容摘要不同")
        self.descriptor = FrozenAdapter(descriptor.kind, manifest, descriptor.fingerprint,
                                        descriptor.release_id, descriptor.generation)
        self.reference = self.descriptor.program
        if not self.reference:
            raise CollectionError("code_release_missing", "未选择代码版本，不启动独立执行")
        self.task_id = uuid.UUID(str(task_id))
        self.source_run_id = uuid.UUID(str(source_run_id))
        self.binding = deepcopy(binding_snapshot)
        self.binding_id = uuid.UUID(str(binding_snapshot["id"]))
        self.attempt = expected_attempt
        self.client = client or AdapterRunnerClient()
        self.wait_seconds = max(0.01, min(float(wait_seconds), 60))
        self.poll_seconds = max(0.01, min(float(poll_seconds), 1))
        self.sleep = sleep
        self.scope_guard = scope_guard
        self.run_ids = set()
        self.errors = []
        with session_factory()() as db:
            self._guard(db)
            package = db.scalar(select(AdapterCodePackage).where(
                AdapterCodePackage.bundle_sha == self.reference["bundle_sha256"]))
            if not package or package.kind != descriptor.kind or package.lock_sha != self.reference["lock_sha256"]:
                raise CollectionError("code_package_unavailable", "固定版本的代码和依赖包不可用")
            bundle = load_bundle(package.file_ref, expected_sha256=package.bundle_sha)
            if content_sha256(bundle["lock"]) != self.reference["lock_sha256"]:
                raise CollectionError("code_package_unavailable", "实际依赖锁与冻结版本不同")
            db.commit()

    def _guard(self, db):
        task = active_execution(db, self.task_id, self.attempt)
        if task.started_at and (now() - task.started_at).total_seconds() > get_settings().task_timeout_seconds:
            raise CollectionError("time_budget", "任务已达到总时间预算")
        source = db.get(SourceRun, self.source_run_id, populate_existing=True)
        binding = db.get(SourceBinding, self.binding_id, populate_existing=True)
        company = db.get(Company, task.company_id, populate_existing=True)
        if not source or source.task_id != task.id or source.binding_id != self.binding_id:
            raise CollectionError("code_runtime_scope", "代码执行与原来源任务不匹配")
        if task.domain != "recruitment" or task.kind != "company_collection" or source.status != "running":
            raise CollectionError("code_runtime_scope", "代码解析仅属于正在执行的招聘来源任务")
        if not company or company.archived or not binding or not binding.enabled or binding.company_id != task.company_id:
            raise CollectionError("source_changed", "来源已停用或不属于当前公司")
        snapshot = next((item for item in task.config_snapshot.get("sources_snapshot", [])
                         if item.get("id") == str(self.binding_id)), None)
        if not snapshot or snapshot != self.binding:
            raise CollectionError("code_runtime_scope", "代码执行没有使用任务原始来源快照")
        frozen = task.config_snapshot.get("adapter_versions", {}).get(str(self.binding_id))
        expected = {"kind": self.descriptor.kind, "release_id": self.descriptor.release_id,
                    "generation": self.descriptor.generation, "fingerprint": self.descriptor.fingerprint}
        if frozen != expected:
            raise CollectionError("adapter_version_conflict", "代码调用与本任务固定版本不同")
        for field in ("kind", "entry_url", "external_company_id", "account_id", "adapter"):
            if field in self.binding and str(getattr(binding, field) or "") != str(self.binding.get(field) or ""):
                raise CollectionError("source_changed", "来源配置已变化，停止旧代码执行")
        if self.scope_guard is not None:
            # Only a first-party checkpoint callback may recognize this task's own saved plan.
            # Task/source identity and immutable URL/account fields were checked above.
            self.scope_guard(db)
        elif "config" in self.binding and binding.config != self.binding["config"]:
            raise CollectionError("source_changed", "来源规则已变化，停止旧代码执行")
        if not task.heartbeat_at or (now() - task.heartbeat_at).total_seconds() >= 2:
            task.heartbeat_at = now()
        return source

    def check(self):
        """Fence first-party browser actions before any new navigation or scrolling."""
        with session_factory()() as db:
            self._guard(db)
            db.commit()

    def _record(self, operation, payload):
        digest = content_sha256(payload)
        # Pure parsing may be reused by a newer owner of the same source task if
        # its public input and immutable code are identical. Ownership still fences every write.
        key = content_sha256({"task": str(self.task_id), "source": str(self.source_run_id),
                              "adapter": self.descriptor.fingerprint, "operation": operation, "input": digest})
        request = AdapterRunnerClient.run_request(self.reference["bundle_sha256"], operation, payload,
                                                  idempotency_key="runtime:" + key)
        with session_factory()() as db:
            self._guard(db)
            db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "program:" + key})
            item = db.scalar(select(AdapterProgramExecution).where(AdapterProgramExecution.execution_key == key).with_for_update())
            if item:
                if item.request != request or item.request_sha != content_sha256(request):
                    raise CollectionError("code_execution_conflict", "同一执行键对应不同输入，未重复请求")
                if item.owner_attempt > self.attempt:
                    raise CollectionError("stale_execution", "旧执行不能重新取得新代次的代码请求")
                item.owner_attempt = self.attempt
            else:
                count = db.query(AdapterProgramExecution).filter(
                    AdapterProgramExecution.task_id == self.task_id,
                    AdapterProgramExecution.source_run_id == self.source_run_id).count()
                if count >= 300:
                    raise CollectionError("code_execution_budget", "本来源代码解析次数已达到上限")
                item = AdapterProgramExecution(task_id=self.task_id, source_run_id=self.source_run_id,
                    binding_id=self.binding_id, owner_attempt=self.attempt, execution_key=key,
                    adapter_fingerprint=self.descriptor.fingerprint, bundle_sha=self.reference["bundle_sha256"],
                    operation=operation, input_sha=digest, request_sha=content_sha256(request), request=request)
                db.add(item)
                db.flush()
            identifier = item.id
            db.commit()
            return identifier

    def _update(self, identifier, **values):
        with session_factory()() as db:
            self._guard(db)
            item = db.scalar(select(AdapterProgramExecution).where(AdapterProgramExecution.id == identifier)
                             .with_for_update().execution_options(populate_existing=True))
            if not item or item.owner_attempt != self.attempt:
                raise CollectionError("stale_execution", "旧代码执行不能覆盖新代次")
            for key, value in values.items():
                setattr(item, key, value)
            db.commit()

    def _failure(self, identifier, code):
        """Record a current owner's stop reason even when cancellation/scope caused the guard to fail."""
        with session_factory()() as db:
            active_execution(db, self.task_id, self.attempt, allow_cancel=True)
            item = db.scalar(select(AdapterProgramExecution).where(AdapterProgramExecution.id == identifier)
                             .with_for_update().execution_options(populate_existing=True))
            if not item or item.owner_attempt != self.attempt or item.task_id != self.task_id:
                raise CollectionError("stale_execution", "旧代码执行不能记录新代次的错误")
            transient = {"runner_submit_unknown", "runner_unavailable", "runner_wait_timeout", "runner_cleanup_pending"}
            if code not in transient:
                item.status = "unknown" if code == "runner_execution_unknown" else "cancelled" if code in {
                    "cancelled", "runner_execution_cancelled"} else "failed"
            item.error_code = code
            db.commit()

    def _call(self, method, *args, deadline, **kwargs):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CollectionError("runner_wait_timeout", "代码解析等待预算已用完，保留原执行键")
        client = self.client
        if type(client) is AdapterRunnerClient:
            if remaining < 1:
                raise CollectionError("runner_wait_timeout", "剩余等待预算不足一次独立请求，保留原运行键")
            # Bound each HTTP call by the remaining invocation budget without mutating shared settings.
            client = copy(client)
            client.settings = copy(client.settings)
            client.settings.adapter_runner_timeout_seconds = min(
                self.client.settings.adapter_runner_timeout_seconds, max(1, remaining))
        return getattr(client, method)(*args, **kwargs)

    def _execute(self, context, identifier):
        operation, payload = context.request["operation"], context.request
        deadline = time.monotonic() + self.wait_seconds
        while True:
            with session_factory()() as db:
                self._guard(db)
                item = db.scalar(select(AdapterProgramExecution).where(AdapterProgramExecution.id == identifier)
                                 .with_for_update().execution_options(populate_existing=True))
                if item.owner_attempt != self.attempt:
                    raise CollectionError("stale_execution", "代码解析已被新代次接管")
                status, request, remote_id, cached = item.status, deepcopy(item.request), item.remote_run_id, deepcopy(item.result)
                if item.error_code == "runner_cleanup_pending":
                    cached = None
                if status == "prepared":
                    item.status = "submitting"
                db.commit()
            if status == "prepared":
                result = self._call("submit_run", request["bundle_sha256"], operation, payload, deadline=deadline,
                    idempotency_key=request["idempotency_key"])
                if result.get("request_sha256") != content_sha256(request):
                    raise CollectionError("runner_request_binding", "独立执行登记未绑定原始请求")
                remote_id = result["run_id"]
                self._update(identifier, remote_run_id=remote_id, status="running")
            elif remote_id is None:
                known = self._call("lookup_run", request["idempotency_key"], deadline=deadline)
                if known:
                    if known.get("request_sha256") != content_sha256(request):
                        raise CollectionError("runner_request_binding", "未知结果查询返回其他请求")
                    remote_id = known["run_id"]
                    self._update(identifier, remote_run_id=remote_id, status="running")
            if remote_id:
                result = cached if cached else self._call("get_run", remote_id, deadline=deadline)
                if result.get("run_id") != remote_id:
                    raise CollectionError("runner_request_binding", "独立执行返回了其他运行结果")
                if result.get("status") in {"succeeded", "failed", "cancelled", "unknown"}:
                    # Keep the original signed result before interpretation, including a failure.
                    self._update(identifier, result=deepcopy(result), remote_run_id=remote_id,
                                 status="accepted" if status == "accepted" else result["status"])
                    output = self.client.verify_result(result, request=request,
                        lock_sha256=self.reference["lock_sha256"], expected_runtime=self.reference["runtime_sha256"])
                    if result["receipt"].get("dependency_environment_sha256") != self.reference["dependency_environment_sha256"]:
                        raise CollectionError("runner_dependency_environment_changed", "实际依赖环境与发布版本不同")
                    return identifier, remote_id, output
            if time.monotonic() >= deadline:
                raise CollectionError("runner_wait_timeout", "代码解析尚未返回终态，保留原请求与运行键")
            self.sleep(self.poll_seconds)

    def invoke(self, context):
        identifier = None
        try:
            operation = context.request["operation"]
            if context.request.get("kind") != self.descriptor.kind or operation not in {"parse_list", "parse_detail", "navigation"}:
                raise CollectionError("code_runtime_scope", "代码输入平台或操作不匹配")
            identifier = self._record(operation, context.request)
            identifier, remote_id, output = self._execute(context, identifier)
            if operation == "parse_list":
                parsed = validate_list_output(context, output)
            elif operation == "parse_detail":
                parsed = validate_detail_output(context, output)
            else:
                parsed = validate_navigation(context, output)
            self._update(identifier, status="accepted", error_code=None)
            self.run_ids.add(remote_id)
            return parsed
        except CollectionError as exc:
            if exc.code not in self.errors:
                self.errors.append(exc.code)
            if identifier is not None:
                try:
                    self._failure(identifier, exc.code)
                except CollectionError:
                    pass
            raise

    def evidence(self):
        return {"release_id": self.descriptor.release_id, "release_generation": self.descriptor.generation,
                "fingerprint": self.descriptor.fingerprint, "bundle_sha256": self.reference["bundle_sha256"],
                "lock_sha256": self.reference["lock_sha256"], "runtime_sha256": self.reference["runtime_sha256"],
                "verified": bool(self.run_ids) and not self.errors, "run_ids": sorted(self.run_ids),
                "error_code": self.errors[-1] if self.errors else None}
