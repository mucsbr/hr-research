"""Authenticated control-plane client; never runs adapter code on this host."""
import hashlib
import hmac
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from app.adapter_code_package import (
    CONTRACT, canonical_bytes, content_sha256, normalized_dependencies, validate_bundle, validate_dependency_lock,
)
from app.collection import CollectionError
from app.config import get_settings

OPERATIONS = {"parse_list", "parse_detail", "navigation"}
MAX_RESPONSE_BYTES = 5_000_000
KEY_PATTERN = r"[A-Za-z0-9_.:-]{1,160}"
HASH_PATTERN = r"[0-9a-f]{64}"


def _failed(code, message):
    raise CollectionError(code, message)


class AdapterRunnerClient:
    def __init__(self, settings=None, *, transport=None):
        self.settings = settings or get_settings()
        self.transport = transport

    def _configuration(self):
        base = self.settings.adapter_runner_url.rstrip("/")
        token = self.settings.adapter_runner_token.get_secret_value()
        if not base or not token:
            _failed("runner_not_configured", "尚未配置独立代码执行服务，不在本机执行候选")
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment
                     and parsed.path in {"", "/"} and parsed.port != 0)
        except ValueError:
            valid = False
        if not valid:
            _failed("runner_invalid_configuration", "代码执行服务须使用管理员配置的固定服务根地址")
        if self.settings.app_env == "test" and self.transport is None:
            _failed("runner_test_network_forbidden", "自动化测试禁止连接未经模拟的代码执行服务")
        return base, token

    def _request(self, method, path, body=None, *, missing_ok=False, submitting=False):
        base, token = self._configuration()
        content = canonical_bytes(body) if body is not None else None
        if content is not None and len(content) > 2_100_000:
            _failed("runner_input_budget", "执行服务请求超过输入上限")
        try:
            with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                              timeout=max(1, min(90, self.settings.adapter_runner_timeout_seconds))) as client:
                with client.stream(method, base + path, content=content,
                                   headers={"Authorization": "Bearer " + token,
                                            "Content-Type": "application/json"}) as response:
                    if response.status_code == 404 and missing_ok:
                        return None
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            _failed("runner_output_budget", "执行服务响应超过大小上限")
                        chunks.append(chunk)
                    if response.status_code not in {200, 201, 202}:
                        if response.status_code == 409:
                            _failed("runner_request_conflict", "执行服务幂等键或固定产物冲突")
                        if response.status_code in {401, 403}:
                            _failed("runner_authentication_failed", "执行服务认证未通过")
                        _failed("runner_rejected", "执行服务拒绝请求；未复制上游错误正文")
                    try:
                        result = json.loads(b"".join(chunks))
                    except (ValueError, UnicodeError, RecursionError) as exc:
                        raise CollectionError("runner_invalid_response", "执行服务未返回有效JSON") from exc
        except httpx.HTTPError as exc:
            code = "runner_submit_unknown" if submitting else "runner_unavailable"
            message = "执行请求结果未知，保留原幂等键查询；未重新执行" if submitting else "代码执行服务暂不可用"
            raise CollectionError(code, message) from exc
        if not isinstance(result, dict):
            _failed("runner_invalid_response", "执行服务响应必须为对象")
        return result

    def health(self):
        result = self._request("GET", "/v1/health")
        if result.get("contract") != CONTRACT or result.get("status") not in {"ready", "ok"}:
            _failed("runner_not_ready", "执行服务接口或固定运行环境尚未就绪")
        runtime = result.get("runtime_sha256", "")
        if not isinstance(runtime, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", runtime):
            _failed("runner_invalid_response", "执行服务未提供固定运行镜像摘要")
        expected = self.settings.adapter_runner_runtime_digest
        if expected and runtime.removeprefix("sha256:") != expected.removeprefix("sha256:"):
            _failed("runner_runtime_changed", "执行服务运行镜像与已登记摘要不同")
        return result

    def resolve_dependencies(self, dependencies):
        dependencies = normalized_dependencies(dependencies)
        result = self._request("POST", "/v1/dependencies/resolve", {"dependencies": dependencies})
        lock = validate_dependency_lock(result.get("lock"), dependencies=dependencies)
        if result.get("lock_sha256") != content_sha256(lock):
            _failed("runner_lock_corrupt", "可信依赖锁与响应摘要不一致")
        return result

    def upload_bundle(self, bundle):
        validate_bundle(bundle)
        digest = content_sha256(bundle)
        result = self._request("PUT", "/v1/bundles/" + digest, bundle)
        if result.get("bundle_sha256") != digest or result.get("lock_sha256") != content_sha256(bundle["lock"]):
            _failed("runner_bundle_corrupt", "执行服务登记的代码包与上传内容不一致")
        return result

    @staticmethod
    def run_request(bundle_sha256, operation, payload, *, idempotency_key):
        if (not isinstance(bundle_sha256, str) or not re.fullmatch(HASH_PATTERN, bundle_sha256)
                or not isinstance(operation, str) or operation not in OPERATIONS
                or not isinstance(idempotency_key, str) or not re.fullmatch(KEY_PATTERN, idempotency_key)
                or not isinstance(payload, dict)):
            _failed("runner_invalid_request", "代码运行请求参数不正确")
        if len(canonical_bytes(payload)) > 2_000_000:
            _failed("runner_input_budget", "解析资料超过执行输入上限")
        return {"idempotency_key": idempotency_key, "bundle_sha256": bundle_sha256,
                "operation": operation, "input": payload}

    def submit_run(self, bundle_sha256, operation, payload, *, idempotency_key):
        request = self.run_request(bundle_sha256, operation, payload, idempotency_key=idempotency_key)
        result = self._request("POST", "/v1/runs", request, submitting=True)
        if (not isinstance(result.get("run_id"), str) or not re.fullmatch(KEY_PATTERN, result["run_id"])
                or result.get("request_sha256") != content_sha256(request)):
            _failed("runner_invalid_response", "运行登记未绑定原始请求")
        return result

    def get_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(KEY_PATTERN, run_id):
            _failed("runner_invalid_request", "运行标识不正确")
        result = self._request("GET", "/v1/runs/" + quote(run_id, safe=""))
        if result.get("run_id") != run_id:
            _failed("runner_invalid_response", "执行服务返回了其他运行的结果")
        return result

    def lookup_run(self, idempotency_key):
        if not isinstance(idempotency_key, str) or not re.fullmatch(KEY_PATTERN, idempotency_key):
            _failed("runner_invalid_request", "运行幂等键不正确")
        return self._request("GET", "/v1/runs/by-key/" + quote(idempotency_key, safe=""), missing_ok=True)

    def cancel_run(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(KEY_PATTERN, run_id):
            _failed("runner_invalid_request", "运行标识不正确")
        return self._request("DELETE", "/v1/runs/" + quote(run_id, safe=""))

    def verify_result(self, result, *, request, lock_sha256, expected_runtime=None):
        """Only Broker-signed terminal evidence is trusted, never candidate claims."""
        _, token = self._configuration()
        receipt = result.get("receipt")
        if not isinstance(receipt, dict):
            _failed("runner_receipt_missing", "运行缺少独立执行回执")
        signature = receipt.get("signature")
        unsigned = {key: value for key, value in receipt.items() if key != "signature"}
        key = hmac.new(token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
        expected = hmac.new(key, canonical_bytes(unsigned), hashlib.sha256).hexdigest()
        if (receipt.get("signature_algorithm") != "hmac-sha256-v1" or not isinstance(signature, str)
                or not hmac.compare_digest(signature, expected)):
            _failed("runner_receipt_invalid", "执行回执签名未通过，不采信候选自报结果")
        checks = {"run_id": result.get("run_id"), "status": result.get("status"),
                  "error_code": result.get("error_code"),
                  "operation": request["operation"],
                  "request_sha256": content_sha256(request), "input_sha256": content_sha256(request["input"]),
                  "bundle_sha256": request["bundle_sha256"], "lock_sha256": lock_sha256,
                  "output_sha256": content_sha256(result.get("output"))}
        if not checks["run_id"] or any(receipt.get(field) != value for field, value in checks.items()):
            _failed("runner_receipt_mismatch", "执行回执与任务、输入、代码、依赖或输出不一致")
        runtime = receipt.get("runtime_sha256", "")
        expected_runtime = expected_runtime or self.settings.adapter_runner_runtime_digest
        if (not isinstance(runtime, str) or not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", runtime)
                or (expected_runtime and runtime.removeprefix("sha256:") != expected_runtime.removeprefix("sha256:"))):
            _failed("runner_runtime_changed", "执行运行环境与验证时固定镜像不一致")
        for field in ("dependency_environment_sha256", "dependency_installation_sha256"):
            if not isinstance(receipt.get(field), str) or not re.fullmatch(HASH_PATTERN, receipt[field]):
                _failed("runner_dependency_evidence_missing", "执行回执缺少实际离线依赖环境证据")
        cleanup = receipt.get("cleanup")
        if (not isinstance(cleanup, dict) or cleanup.get("container_removed") is not True
                or cleanup.get("process_group_terminated") is not True):
            _failed("runner_cleanup_pending", "隔离执行尚未确认完成清理")
        outcome = result.get("status")
        if outcome == "unknown":
            _failed("runner_execution_unknown", "已验签的执行结果仍未知，保留原运行标识且不自动重跑")
        if outcome == "cancelled":
            _failed("runner_execution_cancelled", "隔离执行已取消，不重新生成候选")
        candidate_errors = {"candidate_exit_nonzero", "oom_killed", "timeout", "output_limit", "stderr_limit",
                            "output_object_required", "body_too_large", "duplicate_json_key", "nonfinite_json",
                            "json_too_deep", "invalid_json"}
        if outcome == "failed" and (
                receipt.get("execution_started") is not True or result.get("error_code") not in candidate_errors):
            _failed("runner_infrastructure_failed", "执行环境或固定产物不可用，不以生成新代码处理")
        if outcome not in {"succeeded", "failed"} or receipt.get("execution_started") is not True:
            _failed("runner_receipt_invalid", "回执缺少实际执行开始及终态证据")
        for field in ("limits", "actual_limits"):
            limits = receipt.get(field)
            if not isinstance(limits, dict):
                _failed("runner_policy_unverified", "缺少实际隔离策略证据")
            if (limits.get("network") != "none" or limits.get("read_only") is not True
                    or limits.get("user") != "65532:65532" or limits.get("cap_drop") != ["ALL"]
                    or limits.get("no_new_privileges") is not True):
                _failed("runner_policy_unverified", "执行隔离策略不满足发布要求")
            for name, maximum in (("pids_limit", 32), ("memory_bytes", 268435456),
                                  ("cpus", 1), ("timeout_seconds", 30)):
                value = limits.get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
                    _failed("runner_policy_unverified", "执行资源限制不满足发布要求")
        if (outcome != "succeeded" or type(receipt.get("exit_code")) is not int
                or receipt["exit_code"] != 0
                or receipt.get("oom_killed") is not False or not isinstance(result.get("output"), dict)):
            _failed("runner_execution_failed", "隔离执行没有成功完成，保留旧版本")
        return result["output"]
