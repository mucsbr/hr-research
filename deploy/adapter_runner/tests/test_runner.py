"""Protocol tests: fake container backend, never execute candidates on this host."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
from pathlib import Path
import sys
import time
import tarfile
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import LIMITS, RunnerError, canonical, digest, parse_json, sign_receipt, validate_run
from resolver import Resolver, pins
from service import Broker


TOKEN = "test-only-token-" + "x" * 40


class FakeBackend:
    image = "sha256:" + "a" * 64

    def __init__(self):
        self.runs = 0
        self.stdout = b'{"records":[]}'

    def execute(self, mode, mounts, cancelled=lambda: False):
        if mode == "install":
            assert all(readonly for _, _, readonly in mounts)
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as archive:
                content = b"trusted fixture environment"
                member = tarfile.TarInfo("environment.txt")
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
            output = stream.getvalue()
        else:
            self.runs += 1
            output = self.stdout
        return {
            "stdout": output,
            "stderr": b"",
            "container_id": "fixture",
            "runtime_sha256": self.image,
            "actual_limits": {},
            "exit_code": 0,
            "oom_killed": False,
            "error_code": None,
            "cleanup": True,
            "duration_ms": 1,
            "raw_stdout_sha256": hashlib.sha256(self.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(b"").hexdigest(),
        }

    def verify_runtime(self):
        return self.image

    def cleanup_owned(self):
        return 0


class FakeResolver:
    def __init__(self, _):
        pass

    def resolve(self, dependencies):
        assert dependencies == []
        return {
            "schema_version": 1,
            "python": "3.12",
            "platform": "linux/amd64",
            "dependencies": [],
            "wheels": [],
            "resolver_policy": "approved-pure-wheels-v1",
        }


class HeldExecutor:
    def submit(self, *_):
        pass


@pytest.fixture
def broker(tmp_path):
    value = Broker(tmp_path, TOKEN, FakeBackend(), FakeResolver)
    yield value
    if hasattr(value.executor, "shutdown"):
        value.executor.shutdown(wait=True)
    for path in tmp_path.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() else 0o644)


def bundle(broker, source=None):
    lock = broker.resolve({"dependencies": []})["lock"]
    value = {
        "schema_version": 1,
        "contract": "python_adapter_v1",
        "kind": "boss",
        "files": {"adapter.py": source or "raise RuntimeError('must never execute in broker')"},
        "dependencies": [],
        "lock": lock,
    }
    broker.put_bundle(digest(value), value)
    return value


def request(value, key="fixture-key"):
    return {
        "idempotency_key": key,
        "bundle_sha256": digest(value),
        "operation": "parse_list",
        "input": {"html": "fixture"},
    }


def finished(broker, run_id):
    for _ in range(100):
        result = broker.get(run_id)
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError("fixture run did not finish")


def test_receipt_signature_binds_every_field():
    original = {"run_id": "r", "cleanup": {"container_removed": True}, "output_sha256": "b" * 64}
    receipt = sign_receipt(original, TOKEN)
    signature = receipt.pop("signature")
    key = hmac.new(TOKEN.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
    assert hmac.compare_digest(
        signature, hmac.new(key, canonical(receipt), hashlib.sha256).hexdigest()
    )
    receipt["cleanup"]["container_removed"] = False
    assert signature != hmac.new(key, canonical(receipt), hashlib.sha256).hexdigest()


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[" * 30 + b"0" + b"]" * 30])
def test_ambiguous_or_unbounded_json_rejected(raw):
    with pytest.raises(RunnerError):
        parse_json(raw)


@pytest.mark.parametrize(
    "dependencies",
    [
        ["requests==2.32.0"],
        ["six>=1"],
        ["six==1.17.0", "six==1.17.0"],
        ["six @ https://evil.invalid/x.whl"],
    ],
)
def test_only_approved_exact_dependencies(dependencies):
    with pytest.raises(RunnerError):
        pins(dependencies)


def test_bundle_hash_and_untrusted_lock_rejected(broker):
    value = bundle(broker)
    with pytest.raises(RunnerError, match="bundle_hash_mismatch"):
        broker.put_bundle("0" * 64, value)
    value["lock"]["python"] = "3.11"
    with pytest.raises(RunnerError, match="untrusted_dependency_lock"):
        broker.put_bundle(digest(value), value)


@pytest.mark.parametrize(
    "filename", ["../adapter.py", "/adapter.py", "a/b.py", "sitecustomize.py", "Dockerfile"]
)
def test_bundle_cannot_supply_execution_configuration(broker, filename):
    value = bundle(broker)
    value["files"][filename] = "x = 1"
    with pytest.raises(RunnerError, match="invalid_filename"):
        broker.put_bundle(digest(value), value)


def test_concurrent_duplicate_submission_executes_once(broker):
    value = bundle(broker)
    req = request(value)
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: broker.submit(req), range(24)))
    assert len({state["run_id"] for state, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    result = finished(broker, results[0][0]["run_id"])
    assert broker.backend.runs == 1 and result["status"] == "succeeded"
    receipt = result["receipt"]
    assert receipt["request_sha256"] == digest(req)
    assert receipt["input_sha256"] == digest(req["input"])
    assert receipt["bundle_sha256"] == digest(value)
    assert receipt["operation"] == "parse_list"
    assert receipt["limits"] == LIMITS
    assert receipt["cleanup"] == {"container_removed": True, "process_group_terminated": True}
    assert receipt["signature"] == sign_receipt(receipt, TOKEN)["signature"]
    assert broker.get(req["idempotency_key"], by_key=True) == result
    with pytest.raises(RunnerError, match="idempotency_conflict"):
        broker.submit({**req, "input": {"different": True}})


def test_restart_records_unknown_and_never_reexecutes(broker):
    value = bundle(broker)
    broker.executor.shutdown()
    broker.executor = HeldExecutor()
    req = request(value)
    state, _ = broker.submit(req)
    broker.recover()
    recovered = broker.get(state["run_id"])
    assert recovered["status"] == "unknown"
    assert recovered["receipt"]["request_sha256"] == digest(req)
    again, created = broker.submit(req)
    assert not created and again == recovered and broker.backend.runs == 0


def test_cancel_before_execution_never_runs_candidate(broker):
    value = bundle(broker)
    broker.executor.shutdown()
    broker.executor = HeldExecutor()
    state, _ = broker.submit(request(value))
    broker.cancel(state["run_id"])
    broker.execute_run(state["run_id"])
    result = broker.get(state["run_id"])
    assert result["status"] == "cancelled" and broker.backend.runs == 0
    assert result["receipt"]["cleanup"]["container_removed"]


@pytest.mark.parametrize("tamper", ["bundle", "dependencies"])
def test_stored_artifact_tamper_prevents_execution(broker, tamper):
    value = bundle(broker)
    target = (
        broker.root
        / ("bundles" if tamper == "bundle" else "dependencies")
        / (digest(value) if tamper == "bundle" else digest(value["lock"]))
    )
    file = target / ("adapter.py" if tamper == "bundle" else "environment.txt")
    file.chmod(0o644)
    file.write_text("modified")
    result = finished(broker, broker.submit(request(value))[0]["run_id"])
    assert result["status"] == "failed" and broker.backend.runs == 0


@pytest.mark.parametrize(
    "stdout",
    [b"{}{}", b'{"x":Infinity}', b"[]", b"x" * 4_000_001],
    ids=["multiple", "infinite", "array", "overlimit"],
)
def test_invalid_candidate_output_not_accepted(broker, stdout):
    value = bundle(broker)
    broker.backend.stdout = stdout
    result = finished(broker, broker.submit(request(value))[0]["run_id"])
    assert result["status"] == "failed" and result["output"] is None


@pytest.mark.parametrize("field", ["command", "image", "mounts", "url", "env"])
def test_request_cannot_override_container_policy(field):
    value = {
        "idempotency_key": "a" * 160,
        "bundle_sha256": "a" * 64,
        "operation": "navigation",
        "input": {},
    }
    validate_run(value)
    with pytest.raises(RunnerError, match="invalid_fields"):
        validate_run({**value, field: "forbidden"})


def test_resolver_rejects_pth_and_verifies_wheel_hash(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("six-1.17.0.dist-info/METADATA", "Name: six\nVersion: 1.17.0\n")
        archive.writestr("evil.pth", "import os")
    data = buffer.getvalue()
    resolver = Resolver(tmp_path)
    resolver.meta = lambda *_: {
        "info": {},
        "urls": [
            {
                "packagetype": "bdist_wheel",
                "filename": "six-1.17.0-py2.py3-none-any.whl",
                "url": "https://files.pythonhosted.org/fixture.whl",
                "digests": {"sha256": hashlib.sha256(data).hexdigest()},
            }
        ],
    }
    resolver.fetch = lambda *_: data
    with pytest.raises(RunnerError, match="unsafe_wheel_member"):
        resolver.resolve(["six==1.17.0"])


def test_terminal_none_is_bound_for_failure_cancel_and_unknown(broker):
    value = bundle(broker)
    broker.backend.stdout = b"not-json"
    failed = finished(broker, broker.submit(request(value, "failed"))[0]["run_id"])
    assert failed["receipt"]["output_sha256"] == digest(None)
    broker.executor.shutdown()
    broker.executor = HeldExecutor()
    cancelled, _ = broker.submit(request(value, "cancelled"))
    broker.cancel(cancelled["run_id"])
    broker.execute_run(cancelled["run_id"])
    stopped = broker.get(cancelled["run_id"])
    assert stopped["receipt"]["output_sha256"] == digest(None)
    assert stopped["receipt"]["execution_started"] is False
    pending, _ = broker.submit(request(value, "unknown"))
    broker.recover()
    unknown = broker.get(pending["run_id"])
    assert unknown["receipt"]["output_sha256"] == digest(None)
    assert unknown["receipt"]["execution_started"] is False


def test_near_output_limit_state_survives_get_and_restart(broker):
    value = bundle(broker)
    broker.backend.stdout = canonical({"text": "x" * 3_999_900})
    result = finished(broker, broker.submit(request(value))[0]["run_id"])
    assert result["status"] == "succeeded"
    assert 4_000_000 < len(canonical(result)) < 4_500_000
    assert "input" not in result and "request" not in result
    broker.recover()
    assert broker.get(result["run_id"]) == result
    assert broker.backend.runs == 1


def test_recovery_quarantines_only_corrupt_row_and_keeps_idempotency(broker):
    value = bundle(broker)
    good = finished(broker, broker.submit(request(value, "good"))[0]["run_id"])
    bad_req = request(value, "bad")
    bad = finished(broker, broker.submit(bad_req)[0]["run_id"])
    with broker.db() as db:
        db.execute("UPDATE runs SET state=? WHERE id=?", (b"{broken", bad["run_id"]))
    broker.recover()
    assert broker.get(good["run_id"]) == good
    quarantined = broker.get(bad["run_id"])
    assert (
        quarantined["status"] == "unknown" and quarantined["error_code"] == "stored_state_corrupt"
    )
    assert quarantined["receipt"]["output_sha256"] == digest(None)
    duplicate, created = broker.submit(bad_req)
    assert duplicate == quarantined and not created and broker.backend.runs == 2
    with broker.db() as db:
        assert db.execute("SELECT COUNT(*) FROM quarantined_states").fetchone()[0] == 1


def test_existing_signed_null_receipt_is_upgraded_without_execution(broker):
    value = bundle(broker)
    broker.backend.stdout = b"invalid"
    result = finished(broker, broker.submit(request(value))[0]["run_id"])
    result["receipt"]["output_sha256"] = None
    result["receipt"] = sign_receipt(result["receipt"], TOKEN)
    with broker.db() as db:
        db.execute("UPDATE runs SET state=? WHERE id=?", (canonical(result), result["run_id"]))
    broker.recover()
    fixed = broker.get(result["run_id"])
    assert fixed["receipt"]["output_sha256"] == digest(None)
    assert fixed["receipt"]["null_output_binding_upgraded"] is True
    assert broker.backend.runs == 1


def test_output_at_depth_limit_survives_state_wrapper(broker):
    value = bundle(broker)
    output = {"leaf": True}
    for _ in range(23):
        output = {"n": output}
    broker.backend.stdout = canonical(output)
    result = finished(broker, broker.submit(request(value))[0]["run_id"])
    assert result["status"] == "succeeded" and result["output"] == output
    broker.recover()
    assert broker.get(result["run_id"])["output"] == output
