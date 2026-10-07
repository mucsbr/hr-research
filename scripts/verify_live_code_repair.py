"""One explicitly authorized real-model repair, with no local candidate execution.

Default: offline preparation only. Only --live can create a QA database and arm
the one lifetime paid request. --check-runner opens a temporary authenticated
SSH tunnel and reads health only. No option resets the paid ledger.
"""

import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import ipaddress
import json
import logging
import os
from pathlib import Path
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
STATE = ROOT / ".runtime" / "live-code-repair"
CONDA = Path("/Users/songbingrong/miniforge3/envs/hr-research-dev")
MODEL = "glm-5.3"
MAX_INPUT = 16000
MAX_OUTPUT = 1200
RUNTIME = "sha256:ae9906fb1090bbb27a7e597ce83ecebcb40155f1aaf22144f605f4d2dddb4362"
BROKER_IMAGE = "sha256:68f708c4e814c7cedd48827d288b8fa203170ebfd18a5ab477218c5a40b5a33f"
REMOTE = "dabing@10.255.200.16"
PRIVATE_VALUES = []
TRAINING_ID = "live-training-fixture"
COMPANY = "合成代码解析验收公司"
BINDING = {"kind": "boss", "entry_url": "https://www.zhipin.com/gongsi/live-qa.html", "external_company_id": "live-qa"}
IDENTITY = {"name": COMPANY, "aliases": []}
UNIT = "岗位职责：这是明确标记的合成解析资料，负责接口维护、数据质量核验和自动化回归；任职要求：熟悉软件工程，不代表真实招聘岗位。"
TRAINING_TEXT = (UNIT * 80)[:3000]


class Stop(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def no_secret(value):
    text = value if isinstance(value, str) else canonical(value).decode()
    if any(secret and secret in text for secret in PRIVATE_VALUES):
        raise Stop("credential_reflection_blocked")


def save(name, value, *, exclusive=False):
    no_secret(value)
    path = STATE / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = path if exclusive else path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        if not exclusive:
            os.replace(target, path)
    finally:
        if not exclusive:
            target.unlink(missing_ok=True)


def load(name):
    path = STATE / name
    return json.loads(path.read_text()) if path.exists() else None


def source_sha():
    files = {}
    for folder in ["backend/app", "backend/migrations", "adapters"]:
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts:
                files[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest(files)


def synthetic_material():
    from app.adapter_code_catalog import catalog_for, failure_case
    from app.adapter_maintenance import builtin_manifest
    from app.adapter_program import capture_program_failure, prepare_program_input
    from app.collection import JobDocument

    manifest = builtin_manifest("boss")
    catalog = catalog_for("boss", manifest)
    job = JobDocument(source_job_id=TRAINING_ID, title="合成训练岗位", recruiting_entity=COMPANY,
                      url="https://www.zhipin.com/job_detail/" + TRAINING_ID + ".html")
    html = '<html><body><h1>合成训练岗位</h1><div class="jd-live-qa">' + TRAINING_TEXT + '</div></body></html>'
    context = prepare_program_input("boss", BINDING, IDENTITY, operation="parse_detail", page_url=job.url, html=html, job=job)
    failure = failure_case("boss", BINDING, IDENTITY, capture_program_failure(context), manifest)
    return {"base_spec": catalog["base_spec"], "cases": [*catalog["evidence_cases"], failure],
            "catalog_sha256": catalog["catalog_sha256"], "failure_id": failure["id"],
            "source_snapshot": {"kind": "boss", "synthetic_parse_fixture": True,
                                "fixture_scope": "CSS parsing only; no real BOSS access or login",
                                "catalog_sha256": catalog["catalog_sha256"]}}


def offline_proposal_view(material):
    """Use create_code_proposal's validators, then a stable disk representation.

    This does not pretend to reproduce PostgreSQL JSONB key ordering. The live
    path must refresh the real row and compare JSON payload semantics before
    freezing the exact database-derived messages for the paid request gate.
    """
    from app.adapter_code_service import _json_size, freeze_evidence_cases, validate_draft_spec

    source = material["source_snapshot"]
    if not isinstance(source, dict) or _json_size(source) > 100000:
        raise Stop("offline_source_snapshot_invalid")
    validated = {"base_spec": validate_draft_spec(material["base_spec"]),
                 "evidence_cases": freeze_evidence_cases(material["cases"]),
                 "source_snapshot": source, "error_code": None}
    return SimpleNamespace(**json.loads(canonical(validated)))


def prompt_semantic_sha(messages):
    """Ignore only JSON object key ordering; preserve system/source/array values."""
    normalized = []
    for message in messages:
        item = dict(message)
        if item.get("role") == "user":
            item["content"] = json.loads(item["content"])
        normalized.append(item)
    return digest(normalized)


def checked_messages(proposal):
    from app.adapter_code_service import proposal_messages

    messages, metadata = proposal_messages(proposal, max_input_chars=MAX_INPUT, return_metadata=True)
    no_secret(messages)
    if sum(len(item["content"]) for item in messages) > MAX_INPUT:
        raise Stop("prompt_input_budget")
    payload = json.loads(messages[1]["content"])
    failures = [case for case in proposal.evidence_cases if case.get("role") == "failure"]
    if len(failures) != 1 or payload.get("failure_cases") != failures:
        raise Stop("prompt_failure_case_incomplete")
    excerpts = [item["text"] for item in payload.get("file_excerpts", []) if item.get("path") == "adapter.py"]
    required = ['selectors = RULES["detail_selectors"]', 'html = document(payload, "dom")',
                'soup = BeautifulSoup(html, "html.parser")', 'for selector in selectors:',
                'nodes = soup.select(selector, limit=2)', 'if len(nodes) == 1 and nodes[0].get_text(" ", strip=True):',
                'fields={"description": claim(nodes[0].get_text(" ", strip=True), selector="")}',
                'raise ValueError("No unique verified job description region")']
    missing = [item for item in required if not any(item in excerpt for excerpt in excerpts)]
    if missing:
        raise Stop("prompt_detail_window_incomplete")
    return messages, {**metadata, "complete_failure_case": True, "complete_detail_extraction_window": True,
                      "prompt_sha256": digest(messages), "prompt_semantic_sha256": prompt_semantic_sha(messages)}


def prepare():
    if load("paid-intent.json") or load("state.json"):
        raise Stop("experiment_already_started_no_reset")
    before = source_sha()
    material = synthetic_material()
    proposal = offline_proposal_view(material)
    messages, context = checked_messages(proposal)
    restarted, _ = checked_messages(offline_proposal_view(json.loads(canonical(material))))
    if restarted != messages:
        raise Stop("offline_roundtrip_prompt_changed")
    if before != source_sha():
        raise Stop("source_changed_during_prepare")
    previous = load("plan.json")
    run_id = previous["run_id"] if previous else uuid.uuid4().hex
    plan = {"schema_version": 1, "run_id": run_id, "database_name": "hr_live_code_qa_" + run_id[:16],
            "source_sha256": before, "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "material_sha256": digest(material), "model": MODEL, "reasoning_effort": "low",
            "max_input_chars": MAX_INPUT, "max_output_tokens": MAX_OUTPUT,
            "lifetime_real_request_limit": 1, "daily_request_limit": 1, "task_call_limit": 1,
            "runtime_sha256": RUNTIME, "context": context, "training_description_chars": len(TRAINING_TEXT),
            "offline_serialization": "production-create-validators-then-canonical-json",
            "live_prompt_freeze": "refresh-real-JSONB-row-then-exact-messages; semantic-compare-to-plan",
            "candidate_execution_on_controller": False, "real_site_requests": 0,
            "held_out_check": "new random JobID and different description, same .jd-live-qa; created only after generation"}
    save("plan.json", plan)
    save("prepared-material.json", material)
    save("prepared-messages.json", messages)
    return {"status": "prepared", "plan": str(STATE / "plan.json"), "run_id": run_id,
            "source_sha256": before, "prompt_sha256": context["prompt_sha256"],
            "prompt_semantic_sha256": context["prompt_semantic_sha256"],
            "input_chars": context["input_chars"], "llm_calls": 0, "database_created": False}


def ssh_environment():
    return {key: value for key, value in os.environ.items()
            if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "TERM", "SSH_AUTH_SOCK", "SSH_AGENT_PID", "TMPDIR"}
            or key.startswith("LC_")}


def ssh(args, *, check=True):
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                             "-o", "ConnectTimeout=10", *args], capture_output=True, text=True,
                            timeout=25, env=ssh_environment())
    if check and result.returncode:
        raise Stop("runner_ssh_failed")
    return result


@contextmanager
def runner_connection():
    from pydantic import SecretStr
    from app.adapter_runner_client import AdapterRunnerClient

    # The token is printed only to captured subprocess stdout, then kept in this
    # control process's memory. Never forward it to the caller or a child argv/env.
    remote_code = '''import json,stat,subprocess
from pathlib import Path
p=Path("/home/dabing/hr-adapter-runner-verification-20261005/runner.env")
assert p.is_file() and not p.is_symlink() and not stat.S_IMODE(p.stat().st_mode)&0o077
values=dict(line.split("=",1) for line in p.read_text().splitlines() if line and not line.startswith("#"))
fmt='{"running":{{json .State.Running}},"health":{{json .State.Health.Status}},"image":{{json .Image}},"ports":{{json .NetworkSettings.Ports}},"ip":{{json (index .NetworkSettings.Networks "hr-intel-verify-20261005_runner_control").IPAddress}}}'
info=json.loads(subprocess.check_output(["docker","inspect","--format",fmt,"hr-adapter-runner-verify-20261005-broker-1"],text=True))
assert info["running"] and info["health"]=="healthy" and not any(info["ports"].values())
print(json.dumps({**info,"token":values["RUNNER_TOKEN"],"runtime":values["RUNNER_RUNTIME_IMAGE"]}))
'''
    received = ssh([REMOTE, shlex.join(["python3", "-c", remote_code])])
    secret = json.loads(received.stdout)
    received.stdout = ""
    address = ipaddress.ip_address(secret["ip"])
    if (secret["image"] != BROKER_IMAGE or secret["runtime"] != RUNTIME or not address.is_private
            or address.is_loopback or address.is_link_local or address.version != 4 or len(secret["token"]) < 32):
        raise Stop("runner_identity_mismatch")
    PRIVATE_VALUES.append(secret["token"])
    with tempfile.TemporaryDirectory(prefix="hr-live-runner-", dir="/tmp") as folder:
        control = str(Path(folder) / "s")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = SimpleNamespace(app_env="development", adapter_runner_url=f"http://127.0.0.1:{port}",
            adapter_runner_token=SecretStr(secret["token"]), adapter_runner_runtime_digest=RUNTIME,
            adapter_runner_timeout_seconds=45)
        client = AdapterRunnerClient(settings)
        try:
            ssh(["-M", "-S", control, "-o", "ControlPersist=no", "-o", "ExitOnForwardFailure=yes",
                 "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-fNT",
                 "-L", f"127.0.0.1:{port}:{address}:8090", REMOTE])
            health = client.health()
            yield client, settings, health
        finally:
            try:
                ssh(["-S", control, "-O", "exit", REMOTE], check=False)
                if ssh(["-S", control, "-O", "check", REMOTE], check=False).returncode == 0:
                    raise Stop("runner_tunnel_cleanup_failed")
            finally:
                settings.adapter_runner_token = SecretStr("")
                secret["token"] = ""


def verify_plan():
    plan = load("plan.json")
    material = load("prepared-material.json")
    if not plan or not material:
        raise Stop("prepare_required")
    if (plan["source_sha256"] != source_sha() or plan["driver_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or plan["material_sha256"] != digest(material) or plan["lifetime_real_request_limit"] != 1):
        raise Stop("prepared_source_changed")
    return plan, material


def verify_prepared_offline():
    plan, material = verify_plan()
    messages, metadata = checked_messages(offline_proposal_view(material))
    if (digest(messages) != plan["context"]["prompt_sha256"]
            or prompt_semantic_sha(messages) != plan["context"]["prompt_semantic_sha256"]):
        raise Stop("prepared_prompt_changed")
    result = {"status": "offline_prepared_verified", "run_id": plan["run_id"],
              "prompt_sha256": metadata["prompt_sha256"], "prompt_semantic_sha256": metadata["prompt_semantic_sha256"],
              "input_chars": metadata["input_chars"], "database_connections": 0, "ssh_calls": 0, "llm_calls": 0,
              "postgres_key_order_assumed": False, "live_refresh_still_required": True}
    save("offline-serialization-check.json", result)
    return result


def configure_qa(plan, runner_settings):
    from dotenv import dotenv_values
    from pydantic import SecretStr
    from sqlalchemy.engine import make_url
    from app.config import get_settings
    from app.db import get_engine

    path = ROOT / ".env"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise Stop("local_private_env_required")
    values = dotenv_values(path, interpolate=False)
    key = values.get("LLM_API_KEY") or ""
    base = values.get("LLM_BASE_URL") or ""
    if not key or not base or "${" in key or "${" in base:
        raise Stop("local_model_configuration_missing")
    from urllib.parse import urlsplit
    endpoint = urlsplit(base)
    if endpoint.scheme not in {"https", "http"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise Stop("model_endpoint_invalid")
    original = make_url(values.get("DATABASE_URL") or "")
    if not original.drivername.startswith("postgresql") or original.host not in {"127.0.0.1", "localhost", "::1"}:
        raise Stop("local_postgres_required")
    PRIVATE_VALUES.extend(value for name, value in values.items() if value and len(value) >= 8
                          and any(part in name for part in ("KEY", "TOKEN", "PASSWORD", "SECRET")))
    if original.password:
        PRIVATE_VALUES.append(original.password)
    if get_engine.cache_info().currsize:
        raise Stop("database_engine_initialized_before_qa")
    settings = get_settings()
    settings.app_env = "development"
    settings.database_url = SecretStr(original.set(database=plan["database_name"]).render_as_string(hide_password=False))
    settings.test_database_url = SecretStr("")
    settings.redis_url = SecretStr("")
    settings.llm_base_url, settings.llm_api_key = base, SecretStr(key)
    settings.llm_model, settings.llm_fallback_model, settings.llm_reasoning_effort = MODEL, "", "low"
    settings.llm_daily_request_limit = settings.llm_task_call_limit = 1
    settings.llm_max_input_chars, settings.llm_max_output_tokens = MAX_INPUT, MAX_OUTPUT
    settings.llm_timeout_seconds = 90
    settings.smtp_host = settings.discovery_internal_search_url = ""
    settings.app_secret = settings.admin_password = settings.encryption_key = SecretStr("")
    settings.storage_dir = STATE / plan["run_id"] / "artifacts"
    settings.browser_profiles_dir = STATE / plan["run_id"] / "browser-profiles"
    settings.adapter_runner_url = runner_settings.adapter_runner_url
    settings.adapter_runner_token = runner_settings.adapter_runner_token
    settings.adapter_runner_runtime_digest = RUNTIME
    settings.adapter_runner_timeout_seconds = 45
    return settings, original


def create_database(plan, original):
    import psycopg
    from psycopg import sql
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from app.db import session_factory

    if load("state.json"):
        raise Stop("experiment_already_started_no_automatic_resume")
    save("state.json", {"run_id": plan["run_id"], "database_name": plan["database_name"],
                        "source_sha256": plan["source_sha256"], "status": "database_creation_intent"}, exclusive=True)
    with psycopg.connect(host=original.host, port=original.port or 5432, user=original.username,
                         password=original.password, dbname="postgres", autocommit=True) as connection:
        if connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (plan["database_name"],)).fetchone():
            raise Stop("qa_database_already_exists")
        connection.execute(sql.SQL("CREATE DATABASE {}" ).format(sql.Identifier(plan["database_name"])))
    configuration = Config(str(ROOT / "backend/alembic.ini"))
    configuration.set_main_option("script_location", str(ROOT / "backend/migrations"))
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        command.upgrade(configuration, "head")
    with session_factory()() as db:
        if db.scalar(text("SELECT current_database()")) != plan["database_name"]:
            raise Stop("qa_database_identity_mismatch")
        db.execute(text("CREATE TABLE live_code_qa_marker (run_id text PRIMARY KEY, source_sha text NOT NULL)"))
        db.execute(text("INSERT INTO live_code_qa_marker VALUES (:id,:sha)"), {"id": plan["run_id"], "sha": plan["source_sha256"]})
        db.commit()


def call_audit():
    from sqlalchemy import select
    from app.db import session_factory
    from app.models import ModelCall
    with session_factory()() as db:
        return [{"id": str(row.id), "task_id": str(row.task_id), "model": row.model, "purpose": row.purpose,
                 "status": row.status, "input_chars": row.input_chars, "max_output_tokens": row.max_output_tokens,
                 "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
                 "reasoning_tokens": row.reasoning_tokens, "finish_reason": row.finish_reason, "error_code": row.error_code}
                for row in db.scalars(select(ModelCall))]


def paid_gateway(plan, proposal_id, task_id, expected_messages, settings):
    import httpx
    from app.llm import ModelError, ModelGateway

    expected_sha = digest(expected_messages)
    expected_url = str(httpx.URL(settings.llm_base_url.rstrip("/") + "/chat/completions"))

    class OneRequestTransport(httpx.BaseTransport):
        def __init__(self):
            self.actual = httpx.HTTPTransport(retries=0, trust_env=False)

        def handle_request(self, request):
            payload = json.loads(request.content)
            if (request.method != "POST" or str(request.url) != expected_url or payload.get("model") != MODEL
                    or payload.get("reasoning_effort") != "low" or payload.get("max_tokens") != MAX_OUTPUT
                    or payload.get("stream") is not False or digest(payload.get("messages")) != expected_sha):
                raise ModelError("qa_request_policy", "QA拒绝不符合唯一请求计划的模型请求")
            calls = call_audit()
            if len(calls) != 1 or calls[0]["task_id"] != str(task_id) or calls[0]["status"] != "reserved":
                raise ModelError("qa_model_audit_missing", "没有唯一持久模型审计，未发送请求")
            try:
                save("http-request-intent.json", {"run_id": plan["run_id"], "model_call_id": calls[0]["id"],
                    "request_payload_sha256": digest(payload), "model": MODEL, "count": 1,
                    "created_at": datetime.now(timezone.utc).isoformat()}, exclusive=True)
            except FileExistsError as exc:
                raise ModelError("qa_request_already_attempted", "累计请求预算已经占用，未重发") from exc
            response = self.actual.handle_request(request)
            try:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise ModelError("qa_response_too_large", "模型响应超过QA接收预算")
                    chunks.append(chunk)
                body = b"".join(chunks)
                text = body.decode("utf-8", errors="replace")
                try:
                    decoded = json.loads(text)
                except ValueError:
                    decoded = text
                try:
                    no_secret(text)
                    no_secret(decoded)
                except Stop as exc:
                    raise ModelError("qa_credential_reflection", "上游响应包含敏感值，未落地") from exc
                headers = {key: value for key, value in response.headers.items()
                           if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                return httpx.Response(response.status_code, headers=headers, content=body)
            finally:
                response.close()

        def close(self):
            self.actual.close()

    class Once:
        def complete(self, messages, *, purpose, task_id: uuid.UUID | None = None, **kwargs):
            if (purpose != "adapter_code_upgrade" or str(task_id) != str(expected_task)
                    or digest(messages) != expected_sha or kwargs.get("max_output_tokens") != MAX_OUTPUT
                    or kwargs.get("stream", False) or call_audit()):
                raise ModelError("qa_generation_policy", "付费前QA范围或提示校验失败")
            no_secret(messages)
            try:
                save("paid-intent.json", {"run_id": plan["run_id"], "proposal_id": str(proposal_id),
                    "task_id": str(task_id), "model": MODEL, "reasoning_effort": "low", "count": 1,
                    "prompt_sha256": expected_sha, "max_input_chars": MAX_INPUT, "max_output_tokens": MAX_OUTPUT,
                    "created_at": datetime.now(timezone.utc).isoformat()}, exclusive=True)
            except FileExistsError as exc:
                raise ModelError("qa_paid_budget_spent", "固定实验已有付费意图，禁止再次调用") from exc
            response = ModelGateway(transport=OneRequestTransport()).complete(
                messages, purpose=purpose, task_id=task_id, stream=False, max_output_tokens=MAX_OUTPUT, model=MODEL)
            no_secret(response.content)
            save("model-output.json", {"model_call_id": str(response.call_id), "model": response.model,
                "content": response.content, "content_sha256": hashlib.sha256(response.content.encode()).hexdigest()})
            return response

    expected_task = task_id
    return Once()


def held_out_runtime(plan, release_id, client, paid_messages):
    from sqlalchemy import func, select
    from app.adapter_code_runtime import ProgramRuntime, freeze_for_task
    from app.adapter_program import prepare_program_input
    from app.db import session_factory
    from app.ingest import ingest
    from app.models import Company, JobObservation, JobSourceRecord, SourceBinding, SourceRun, Subject, TaskRun, now
    from app.collection import CollectionResult
    from app.serialization import row

    # Alphabetic nonces cannot accidentally match the production phone/ID redactor.
    alphabet = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
    new_id = "heldout-" + uuid.uuid4().hex[:16].translate(alphabet)
    description = "岗位职责：这是一条未参与模型生成的合成解析资料，负责库存事件管道、幂等写入和离线校验。任职要求：能够设计审计接口及异常恢复流程。独立标记：" + uuid.uuid4().hex.translate(alphabet)
    prompt_text = canonical(paid_messages).decode()
    assert new_id not in prompt_text and description not in prompt_text and description != TRAINING_TEXT
    with session_factory()() as db:
        subject = Subject(name=COMPANY)
        db.add(subject)
        db.flush()
        company = Company(subject_id=subject.id, name=COMPANY, role="other",
                          notes="Synthetic parsing fixture only; no BOSS access/login; run=" + plan["run_id"])
        db.add(company)
        db.flush()
        binding = SourceBinding(company_id=company.id, **BINDING)
        db.add(binding)
        db.flush()
        snapshot = row(binding)
        task = TaskRun(company_id=company.id, domain="recruitment", kind="company_collection", status="running",
                       attempt=1, started_at=now(), heartbeat_at=now(), scope_hash=digest({"run": plan["run_id"], "kind": "heldout"}),
                       config_snapshot={"sources_snapshot": [snapshot], "company_identity": IDENTITY})
        db.add(task)
        db.flush()
        source = SourceRun(task_id=task.id, binding_id=binding.id, status="running", scope_hash=task.scope_hash)
        db.add(source)
        db.commit()
        task_id, source_id, binding_id = task.id, source.id, binding.id
    descriptor = freeze_for_task("boss", task_id=task_id, binding_id=binding_id, expected_attempt=1)
    assert descriptor.release_id == str(release_id)
    runtime = ProgramRuntime(descriptor, task_id=task_id, source_run_id=source_id,
                             binding_snapshot=snapshot, expected_attempt=1, client=client)
    payload = {"zpData": {"jobList": [{"encryptJobId": new_id, "jobName": "独立库存研发解析夹具",
                "brandName": COMPANY, "encryptBrandId": "live-qa", "cityName": "上海", "salaryDesc": "20-30K"}]}}
    context = prepare_program_input("boss", snapshot, IDENTITY, operation="parse_list", page_url=BINDING["entry_url"], payload=payload)
    parsed = runtime.invoke(context)
    assert len(parsed.records) == 1 and parsed.records[0].source_job_id == new_id
    job = parsed.records[0]
    html = '<html><body><h1>独立库存研发解析夹具</h1><div class="jd-live-qa">' + description + '</div></body></html>'
    detail = prepare_program_input("boss", snapshot, IDENTITY, operation="parse_detail", page_url=job.url, html=html, job=job)
    document, _ = runtime.invoke(detail)
    assert document.source_job_id == new_id and document.description == description
    document.observed_at = now()
    proof = runtime.evidence()
    assert proof["verified"] and len(proof["run_ids"]) == 2
    with session_factory()() as db:
        task, source, binding = db.get(TaskRun, task_id), db.get(SourceRun, source_id), db.get(SourceBinding, binding_id)
        result = CollectionResult(records=[document], coverage="partial", pages=1,
            documents=[(document.url, document.model_dump_json(), "application/json")],
            stop_reason="Synthetic parsing fixture; no real BOSS page accessed", code_execution=proof,
            adapter_fingerprint=descriptor.fingerprint)
        counts = ingest(db, task, binding, source, result)
        source.counts = {**counts, "code_execution": proof, "synthetic_parse_fixture": True}
        source.status, source.coverage, source.finished_at = "partial", "partial", now()
        task.status, task.finished_at = "partial", now()
        db.commit()
        assert db.scalar(select(func.count()).select_from(JobObservation).where(JobObservation.source_run_id == source_id)) == 1
        record = db.scalar(select(JobSourceRecord).where(JobSourceRecord.binding_id == binding_id))
        assert record.source_job_id == new_id and record.data["description"] == description
    return {"source_run_id": str(source_id), "task_id": str(task_id), "job_id": new_id,
            "description_sha256": hashlib.sha256(description.encode()).hexdigest(), "persisted_observations": 1,
            "code_execution": proof, "held_out_input_not_in_prompt": True,
            "synthetic_parse_fixture": True, "real_site_requests": 0, "platform_logins": 0}


def live():
    if Path(sys.prefix).resolve() != CONDA.resolve():
        raise Stop("required_conda_environment")
    if load("paid-intent.json") or load("state.json"):
        raise Stop("experiment_already_started_no_automatic_resume")
    plan, material = verify_plan()
    fake = offline_proposal_view(material)
    messages, _ = checked_messages(fake)
    if digest(messages) != plan["context"]["prompt_sha256"]:
        raise Stop("prepared_prompt_changed")
    with runner_connection() as (client, runner_settings, health):
        settings, original = configure_qa(plan, runner_settings)
        try:
            create_database(plan, original)
            from app.adapter_code_models import AdapterCodeProposal
            from app.adapter_code_service import create_code_proposal, generate_code_candidate, advance_code_validation
            from app.adapter_code_release import publish_code_proposal
            from app.adapter_maintenance import ensure_registry
            from app.db import session_factory

            with session_factory()() as db:
                registry = ensure_registry(db, "boss")
                registry.code_agent_enabled = True
                proposal = create_code_proposal(db, "boss", base_spec=material["base_spec"],
                    source_snapshot=material["source_snapshot"], evidence_cases=material["cases"],
                    origin="manual", request_key="live-qa:" + plan["run_id"], max_candidates=1)
                db.commit()
                # expire_on_commit=False retains the constructor's Python order.
                # Refresh now so this exact prompt matches the production
                # generator's later expire_all()/JSONB reload before any charge.
                db.refresh(proposal)
                expected, metadata = checked_messages(proposal)
                if prompt_semantic_sha(expected) != plan["context"]["prompt_semantic_sha256"]:
                    raise Stop("database_prompt_semantics_changed")
                proposal_id, task_id = proposal.id, proposal.task_id
                base_release_id, base_generation = proposal.base_release_id, proposal.base_generation
            save("database-preflight-messages.json", expected)
            save("database-preflight-prompt.json", {"origin": "refreshed_postgresql_jsonb_row",
                "proposal_id": str(proposal_id), "task_id": str(task_id),
                "prompt_sha256": digest(expected), "prompt_semantic_sha256": prompt_semantic_sha(expected),
                "offline_prompt_sha256": plan["context"]["prompt_sha256"], "input_chars": metadata["input_chars"]})
            save("state.json", {"run_id": plan["run_id"], "database_name": plan["database_name"],
                "source_sha256": plan["source_sha256"], "proposal_id": str(proposal_id), "task_id": str(task_id),
                "status": "ready_for_unique_model_call", "prompt_metadata": metadata})
            gateway = paid_gateway(plan, proposal_id, task_id, expected, settings)
            generate_code_candidate(proposal_id, gateway=gateway, client=client)
            audit = call_audit()
            save("model-call-audit.json", audit)
            if len(audit) != 1 or audit[0]["status"] != "completed":
                raise Stop("model_did_not_complete_no_retry")
            deadline = time.monotonic() + 240
            while True:
                with session_factory()() as db:
                    proposal = db.get(AdapterCodeProposal, proposal_id)
                    status, error_code = proposal.status, proposal.error_code
                if status == "validated":
                    break
                if status not in {"draft_ready", "resolving", "resolved", "uploading", "validating"}:
                    raise Stop(error_code or "candidate_validation_stopped")
                if time.monotonic() >= deadline:
                    raise Stop("validation_deadline_no_new_model")
                step = advance_code_validation(proposal_id, client=client)
                if step.get("error_code"):
                    raise Stop(step["error_code"])
                with session_factory()() as db:
                    proposal = db.get(AdapterCodeProposal, proposal_id)
                    if proposal.error_code:
                        raise Stop(proposal.error_code)
                time.sleep(0.25)
            with session_factory()() as db:
                proposal = db.get(AdapterCodeProposal, proposal_id)
                save("validation.json", {"proposal_id": str(proposal.id), "status": proposal.status,
                    "bundle_sha256": proposal.bundle_sha, "runs": proposal.validation_run_refs,
                    "candidate_history": proposal.candidate_history})
                release = publish_code_proposal(db, proposal_id, expected_active_release_id=base_release_id,
                                               expected_generation=base_generation, client=client)
                db.commit()
                release_id = release.id
            heldout = held_out_runtime(plan, release_id, client, expected)
            assert len(call_audit()) == 1 and source_sha() == plan["source_sha256"]
            summary = {"status": "passed", "run_id": plan["run_id"], "source_sha256": plan["source_sha256"],
                "database_name": plan["database_name"], "model": MODEL, "reasoning_effort": "low",
                "real_model_requests": 1, "model_call_audit": call_audit(), "max_candidates": 1,
                "proposal_id": str(proposal_id), "release_id": str(release_id), "held_out": heldout,
                "runtime_sha256": health["runtime_sha256"], "candidate_execution_on_controller": False,
                "real_boss_access": False, "main_database_writes": 0}
            save("summary.json", summary)
            return summary
        finally:
            from pydantic import SecretStr
            settings.llm_api_key = settings.adapter_runner_token = SecretStr("")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="Root-only authorized execution; lifetime maximum one model request")
    modes.add_argument("--check-runner", action="store_true", help="Temporary SSH/health only; no local model key or database")
    modes.add_argument("--verify-prepared", action="store_true", help="Offline restart/serialization check; no credentials, database or HTTP")
    args = parser.parse_args()
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = STATE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise Stop("private_experiment_directory_required")
    descriptor = os.open(STATE / "controller.lock", os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Stop("another_controller_is_running") from exc
        logging.disable(logging.CRITICAL)
        if args.live:
            result = live()
            print(json.dumps({key: result[key] for key in ["status", "run_id", "real_model_requests", "candidate_execution_on_controller"]}))
        elif args.verify_prepared:
            print(json.dumps(verify_prepared_offline()))
        elif args.check_runner:
            with runner_connection() as (_, _, health):
                result = {"status": "runner_ready", "health": health, "llm_calls": 0, "database_created": False}
            result["ssh_tunnel_closed"] = True
            save("runner-readiness.json", result)
            print(json.dumps(result))
        else:
            print(json.dumps(prepare(), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = error.code if isinstance(error, Stop) else getattr(error, "code", type(error).__name__)
        result = {"status": "stopped", "error_code": code,
                  "paid_intent_exists": (STATE / "paid-intent.json").exists(),
                  "http_intent_exists": (STATE / "http-request-intent.json").exists(),
                  "automatic_retry": False, "candidate_execution_on_controller": False}
        try:
            save("last-stop.json", result)
        except Exception:
            pass
        print(json.dumps(result))
        raise SystemExit(1) from None
