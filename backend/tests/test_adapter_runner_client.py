from copy import deepcopy
import hashlib
import hmac
from types import SimpleNamespace

import httpx
from pydantic import SecretStr
import pytest

from app.adapter_code_package import canonical_bytes, content_sha256
from app.adapter_runner_client import AdapterRunnerClient
from app.collection import CollectionError

TOKEN = "test-runner-token-not-a-real-secret"
RUNTIME = "sha256:" + "a" * 64
LIMITS = {"network": "none", "read_only": True, "user": "65532:65532", "cap_drop": ["ALL"],
          "no_new_privileges": True, "pids_limit": 32, "memory_bytes": 268435456,
          "cpus": 1, "timeout_seconds": 30}


def settings(**kwargs):
    return SimpleNamespace(app_env="test", adapter_runner_url="http://runner.example.invalid",
                           adapter_runner_token=SecretStr(TOKEN), adapter_runner_timeout_seconds=2,
                           adapter_runner_runtime_digest=kwargs.get("runtime", RUNTIME))


def client(handler=None):
    return AdapterRunnerClient(settings(), transport=httpx.MockTransport(handler or (lambda _: httpx.Response(500))))


def request():
    return AdapterRunnerClient.run_request("b" * 64, "parse_list", {"document": "public sample"},
                                          idempotency_key="proposal:1:case:1")


def sign(receipt):
    unsigned = {key: value for key, value in receipt.items() if key != "signature"}
    key = hmac.new(TOKEN.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
    receipt["signature"] = hmac.new(key, canonical_bytes(unsigned), hashlib.sha256).hexdigest()


def result():
    req, output = request(), {"records": []}
    receipt = {"run_id": "run-1", "status": "succeeded", "operation": req["operation"],
               "request_sha256": content_sha256(req), "input_sha256": content_sha256(req["input"]),
               "bundle_sha256": req["bundle_sha256"], "lock_sha256": "c" * 64,
               "runtime_sha256": RUNTIME, "output_sha256": content_sha256(output),
               "dependency_environment_sha256": "d" * 64, "dependency_installation_sha256": "e" * 64,
               "limits": deepcopy(LIMITS), "actual_limits": deepcopy(LIMITS), "exit_code": 0,
               "execution_started": True,
               "oom_killed": False, "cleanup": {"container_removed": True, "process_group_terminated": True},
               "signature_algorithm": "hmac-sha256-v1"}
    sign(receipt)
    return {"run_id": "run-1", "status": "succeeded", "output": output, "receipt": receipt}


def test_test_mode_cannot_contact_a_real_runner():
    with pytest.raises(CollectionError) as caught:
        AdapterRunnerClient(settings()).health()
    assert caught.value.code == "runner_test_network_forbidden"


def test_unknown_submit_is_not_retried_and_retains_queryable_key():
    calls = []
    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            assert req.headers["Authorization"] == "Bearer " + TOKEN
            raise httpx.ReadTimeout("token or untrusted remote details", request=req)
        return httpx.Response(404)
    runner = client(handler)
    req = request()
    with pytest.raises(CollectionError) as caught:
        runner.submit_run(req["bundle_sha256"], req["operation"], req["input"], idempotency_key=req["idempotency_key"])
    assert caught.value.code == "runner_submit_unknown"
    assert "token or" not in str(caught.value)
    assert runner.lookup_run(req["idempotency_key"]) is None
    assert [method for method, _ in calls] == ["POST", "GET"]


def test_signed_receipt_is_required_not_a_candidate_passed_claim():
    runner = client()
    good = result()
    assert runner.verify_result(good, request=request(), lock_sha256="c" * 64) == {"records": []}
    good["output"] = {"passed": True}
    with pytest.raises(CollectionError) as caught:
        runner.verify_result(good, request=request(), lock_sha256="c" * 64)
    assert caught.value.code == "runner_receipt_mismatch"


@pytest.mark.parametrize("mutation", ["signature", "runtime", "network", "cleanup", "request", "status", "limit"])
def test_changed_execution_context_or_unsafe_limits_cannot_validate(mutation):
    value = result()
    receipt = value["receipt"]
    if mutation == "signature":
        receipt["signature"] = "0" * 64
    elif mutation == "runtime":
        receipt["runtime_sha256"] = "sha256:" + "f" * 64
        sign(receipt)
    elif mutation == "network":
        receipt["actual_limits"]["network"] = "bridge"
        sign(receipt)
    elif mutation == "cleanup":
        receipt["cleanup"]["container_removed"] = False
        sign(receipt)
    elif mutation == "request":
        receipt["request_sha256"] = "0" * 64
        sign(receipt)
    elif mutation == "status":
        receipt["status"] = "cancelled"
        sign(receipt)
    else:
        receipt["actual_limits"]["pids_limit"] = 0
        sign(receipt)
    with pytest.raises(CollectionError):
        client().verify_result(value, request=request(), lock_sha256="c" * 64)


def test_untrusted_http_error_text_is_not_propagated():
    runner = client(lambda _: httpx.Response(403, text="secret-token-and-host-config"))
    with pytest.raises(CollectionError) as caught:
        runner.health()
    assert caught.value.code == "runner_authentication_failed"
    assert "secret-token" not in str(caught.value)


def test_health_and_result_identity_are_checked():
    runner = client(lambda _: httpx.Response(200, json={"status": "ready", "contract": "python_adapter_v1",
                                                     "runtime_sha256": RUNTIME}))
    assert runner.health()["runtime_sha256"] == RUNTIME
    with pytest.raises(CollectionError):
        client(lambda _: httpx.Response(200, json={"run_id": "another-run"})).get_run("run-1")


@pytest.mark.parametrize("status,started,error,code", [
    ("unknown", True, "broker_execution_interrupted", "runner_execution_unknown"),
    ("cancelled", False, "cancelled", "runner_execution_cancelled"),
    ("failed", False, "docker_unavailable", "runner_infrastructure_failed"),
    ("failed", True, "docker_operation_failed", "runner_infrastructure_failed"),
    ("failed", True, "runner_execution_error", "runner_infrastructure_failed"),
    ("failed", True, "docker_attach_stuck", "runner_infrastructure_failed"),
    ("failed", True, "candidate_exit_nonzero", "runner_execution_failed"),
])
def test_authenticated_terminal_outcomes_are_distinct_from_receipt_corruption(status, started, error, code):
    value = result()
    value.update(status=status, output=None, error_code=error)
    value["receipt"].update(status=status, output_sha256=content_sha256(None), error_code=error,
                            exit_code=1 if started else None, execution_started=started)
    if not started:
        value["receipt"].pop("actual_limits")
    sign(value["receipt"])
    with pytest.raises(CollectionError) as caught:
        client().verify_result(value, request=request(), lock_sha256="c" * 64)
    assert caught.value.code == code
