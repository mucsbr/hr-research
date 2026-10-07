"""Strict wire format shared by the trusted broker and its tests."""

import hashlib
import hmac
import json
import re

APPROVED = frozenset(
    {
        "beautifulsoup4",
        "soupsieve",
        "typing-extensions",
        "html5lib",
        "six",
        "webencodings",
        "packaging",
    }
)
KINDS = frozenset({"official", "boss", "liepin", "zhaopin", "51job"})
OPERATIONS = frozenset({"parse_list", "parse_detail", "navigation"})
HEX = re.compile(r"^[a-f0-9]{64}$")
STATE_MAX_BYTES = 4_500_000
RESPONSE_MAX_BYTES = 5_000_000
LIMITS = {
    "input_bytes": 2_000_000,
    "stdout_bytes": 4_000_000,
    "stderr_bytes": 65_536,
    "timeout_seconds": 30,
    "cpus": 1,
    "memory_bytes": 268_435_456,
    "pids_limit": 32,
    "nofile": 128,
    "tmpfs_bytes": 100_663_296,
    "network": "none",
    "read_only": True,
    "user": "65532:65532",
    "cap_drop": ["ALL"],
    "no_new_privileges": True,
}


class RunnerError(Exception):
    def __init__(self, code, message=None, status=400):
        self.code, self.message, self.status = code, message or code, status
        super().__init__(self.message)


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def encode_state(value):
    encoded = canonical(value)
    if len(encoded) > STATE_MAX_BYTES:
        raise RunnerError("result_envelope_limit")
    return encoded


def parse_state(raw):
    value = parse_json(raw, STATE_MAX_BYTES, max_depth=32)
    exact_fields(value, {"run_id", "status", "request_sha256", "output", "error_code", "receipt"})
    if value["status"] not in {"queued", "running", "succeeded", "failed", "cancelled", "unknown"}:
        raise RunnerError("invalid_stored_state")
    return value


def parse_json(raw, limit=4_000_000, max_depth=24):
    if len(raw) > limit:
        raise RunnerError("body_too_large", status=413)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RunnerError("duplicate_json_key")
            result[key] = value
        return result

    def constant(_):
        raise RunnerError("nonfinite_json")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)

        def depth(node, level=0):
            if level > max_depth:
                raise RunnerError("json_too_deep")
            if isinstance(node, dict):
                for child in node.values():
                    depth(child, level + 1)
            elif isinstance(node, list):
                for child in node:
                    depth(child, level + 1)

        depth(value)
        canonical(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise RunnerError("invalid_json") from exc


def exact_fields(value, required, optional=()):
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or set(required) - set(value)
    ):
        raise RunnerError("invalid_fields")


def sha(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise RunnerError("invalid_sha256")
    return value


def validate_bundle(bundle):
    exact_fields(bundle, {"schema_version", "contract", "kind", "files", "dependencies", "lock"})
    if (
        type(bundle["schema_version"]) is not int
        or bundle["schema_version"] != 1
        or bundle["contract"] != "python_adapter_v1"
    ):
        raise RunnerError("unsupported_contract")
    if not isinstance(bundle["kind"], str) or bundle["kind"] not in KINDS:
        raise RunnerError("unsupported_kind")
    files = bundle["files"]
    if not isinstance(files, dict) or "adapter.py" not in files or not 1 <= len(files) <= 8:
        raise RunnerError("invalid_files")
    total = 0
    for name, content in files.items():
        if not re.fullmatch(r"(?:lib/)?[A-Za-z_][A-Za-z0-9_]{0,60}\.py", name) or name.rsplit(
            "/", 1
        )[-1] in {"sitecustomize.py", "usercustomize.py", "bootstrap.py"}:
            raise RunnerError("invalid_filename")
        if not isinstance(content, str) or "\x00" in content or len(content.encode()) > 131_072:
            raise RunnerError("invalid_source")
        total += len(content.encode())
    if total > 100_000 or len(canonical(bundle)) > 160_000:
        raise RunnerError("source_too_large", status=413)
    if not isinstance(bundle["dependencies"], list) or not isinstance(bundle["lock"], dict):
        raise RunnerError("invalid_dependencies")


def validate_run(value):
    exact_fields(value, {"idempotency_key", "bundle_sha256", "operation", "input"})
    key = value["idempotency_key"]
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
        raise RunnerError("invalid_idempotency_key")
    sha(value["bundle_sha256"])
    if (
        not isinstance(value["operation"], str)
        or value["operation"] not in OPERATIONS
        or not isinstance(value["input"], dict)
    ):
        raise RunnerError("invalid_operation_or_input")
    if len(canonical(value["input"])) > LIMITS["input_bytes"]:
        raise RunnerError("input_too_large", status=413)


def sign_receipt(receipt, token):
    receipt = {key: value for key, value in receipt.items() if key != "signature"}
    receipt["signature_algorithm"] = "hmac-sha256-v1"
    key = hmac.new(token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
    receipt["signature"] = hmac.new(key, canonical(receipt), hashlib.sha256).hexdigest()
    return receipt
