"""Isolated code-maintenance E2E scaffold; execution requires an explicit source freeze.

Candidates are inert text here. Only the registered Broker executes candidate code.
The only doubles are model responses and the named website's network transport.
"""

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOST = "www.zhipin.com"
LIST_PATH = "/web/geek/job"
JSON_PATH = "/wapi/zpgeek/search/joblist.json"
DESCRIPTION = "岗位职责：负责企业平台研发与系统架构设计，建设稳定的数据处理流程，参与接口开发与性能优化，和产品团队协作完成需求迭代。任职要求：熟悉软件工程和数据库。"
LAYOUTS = {
    "native": "job-sec-text",
    "old_program": "jd-v1",
    "new_program": "jd-v2",
    "bad_candidate": "jd-v3",
}
OLD_ANCHOR = 'selectors = RULES["detail_selectors"]'
OLD_PATCH = 'selectors = [*RULES["detail_selectors"], ".jd-v1"]'
NEW_PATCH = 'selectors = [*RULES["detail_selectors"], ".jd-v1", ".jd-v2"]'
HOOKS = {
    "backend/app/adapter_code_orchestration.py": [
        "record_code_failure",
        "queue_code_from_failure",
        "tick_code_maintenance",
        "prepare_code_recovery",
        "finish_code_recovery",
    ],
    "backend/app/adapter_code_release.py": ["publish_code_proposal"],
    "backend/app/adapter_code_runtime.py": ["freeze_for_task"],
}
MARKERS = {
    "backend/app/workers.py": [
        "record_code_failure",
        "prepare_code_recovery",
        "finish_code_recovery",
    ],
    "backend/app/browser_internal.py": ["runtime_scope", "freeze_for_task", "code_execution"],
    "backend/app/platform_collectors.py": ["ProgramRuntime", "code_execution"],
}
FORMAL_TABLES = (
    "jobs",
    "job_source_records",
    "job_versions",
    "job_observations",
    "job_change_events",
    "raw_documents",
    "notification_events",
    "notification_deliveries",
)
STOPPED_STATUSES = {
    "needs_engineering",
    "unknown",
    "conflict",
    "cancelled",
    "failed",
    "skipped",
    "blocked_configuration",
    "waiting_login",
    "waiting_verification",
    "succeeded",
    "partial",
    "recovered",
}


class QAWaitStopped(RuntimeError):
    code = "qa_unexpected_stop"


def canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append_event(path, value):
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(canonical({"time": time.time(), "pid": os.getpid(), **value}) + b"\n")


def source_manifest():
    paths = []
    for folder in ["backend/app", "backend/migrations", "adapters"]:
        paths += [
            p
            for p in (ROOT / folder).rglob("*")
            if p.is_file() and p.suffix in {".py", ".json"} and "__pycache__" not in p.parts
        ]
    files = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)
    }
    pending = []
    for name, functions in HOOKS.items():
        path = ROOT / name
        defined = (
            {
                node.name
                for node in ast.parse(path.read_text()).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if path.exists()
            else set()
        )
        pending += [name + ":" + function for function in functions if function not in defined]
    for name, markers in MARKERS.items():
        content = (ROOT / name).read_text() if (ROOT / name).exists() else ""
        pending += [name + ":" + marker for marker in markers if marker not in content]
    return {"files": files, "source_sha256": digest(files), "pending_hooks": pending}


def prepare(args):
    manifest = source_manifest()
    if args.freeze and manifest["pending_hooks"]:
        raise RuntimeError(
            "Collector/orchestration hooks are not complete; no environment was started"
        )
    plan = {
        "schema_version": 1,
        "status": "frozen" if args.freeze else "prepared_not_executed",
        **manifest,
        "runtime_sha256": args.runtime_digest,
        "fixture_sha256": digest(
            {
                "layouts": LAYOUTS,
                "description": DESCRIPTION,
                "old_patch": OLD_PATCH,
                "new_patch": NEW_PATCH,
            }
        ),
        "scenario_order": [
            "native_failure_to_old_program",
            "old_program_failure_to_new_program",
            "bad_candidate_keeps_active",
            "cas_rollback_and_real_old_program_collection",
        ],
        "mock_layers": ["adapter_code_service.ModelGateway", "named-website network transport"],
        "real_layers": [
            "PostgreSQL",
            "Redis",
            "Celery Worker",
            "internal HTTP",
            "Playwright DOM",
            "Broker",
            "first-party validation",
            "CAS publication",
            "ingest",
        ],
        "real_llm_calls": 0,
        "requires_dedicated_qa_database": True,
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    save_json(args.freeze_manifest, plan)
    print(
        json.dumps(
            {
                "status": plan["status"],
                "pending_hooks": plan["pending_hooks"],
                "source_sha256": plan["source_sha256"],
                "executed": False,
            }
        )
    )


def guard(args):
    if sys.platform != "linux" or (ROOT / ".env").exists():
        raise RuntimeError("Execution is restricted to a clean, dedicated remote Linux staging")
    frozen = json.loads(args.freeze_manifest.read_text())
    current = source_manifest()
    if (
        frozen.get("status") != "frozen"
        or current["pending_hooks"]
        or frozen.get("source_sha256") != current["source_sha256"]
        or frozen.get("driver_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise RuntimeError("Frozen driver/collector/hooks do not match; changed sources will not run")
    run_id = os.environ.get("HR_CODE_E2E_RUN_ID", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise RuntimeError("A dedicated QA run identity is required")
    database = urlsplit(os.environ.get("DATABASE_URL", ""))
    redis = urlsplit(os.environ.get("REDIS_URL", ""))
    expected_db = "hr_code_qa_" + run_id[:12]
    if (
        database.hostname != "code-e2e-postgres"
        or database.path != "/" + expected_db
        or redis.hostname != "code-e2e-redis"
    ):
        raise RuntimeError("Only the dedicated QA PostgreSQL/Redis endpoints are allowed")
    if (
        os.environ.get("LLM_API_KEY", "")
        or os.environ.get("LLM_DAILY_REQUEST_LIMIT") != "0"
        or os.environ.get("SMTP_HOST", "")
    ):
        raise RuntimeError("QA requires empty model key, zero daily budget, and no SMTP host")
    if os.environ.get("ADAPTER_RUNNER_RUNTIME_DIGEST") != frozen.get("runtime_sha256"):
        raise RuntimeError("Runner runtime does not match the frozen test plan")
    if os.environ.get("BROWSER_SERVICE_URL") != "http://127.0.0.1:18091":
        raise RuntimeError("Only the independent QA API loopback endpoint is allowed")
    state = args.state_dir.resolve()
    if run_id not in str(state) or not all(
        Path(os.environ.get(key, "/forbidden")).resolve().is_relative_to(state)
        for key in ["STORAGE_DIR", "BROWSER_PROFILES_DIR"]
    ):
        raise RuntimeError("All application artifacts must stay inside this QA run directory")
    state.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "backend"))
    return SimpleNamespace(run_id=run_id, database=expected_db, state=state, frozen=frozen)


def database_guard(context, *, create_marker=False):
    from sqlalchemy import inspect, text
    from app.db import session_factory

    with session_factory()() as db:
        if db.scalar(text("SELECT current_database()")) != context.database:
            raise RuntimeError("Connected database is not the declared QA database")
        required = {
            "adapter_code_proposals",
            "adapter_code_packages",
            "adapter_program_executions",
            "adapter_registries",
        }
        if not required.issubset(inspect(db.bind).get_table_names()):
            raise RuntimeError("Run the frozen product migrations on the dedicated QA DB first")
        if create_marker:
            db.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS code_e2e_marker (run_id text PRIMARY KEY, source_sha text NOT NULL)"
                )
            )
            rows = db.execute(text("SELECT run_id,source_sha FROM code_e2e_marker")).all()
            if not rows:
                db.execute(
                    text("INSERT INTO code_e2e_marker VALUES (:id,:sha)"),
                    {"id": context.run_id, "sha": context.frozen["source_sha256"]},
                )
                db.commit()
        rows = db.execute(text("SELECT run_id,source_sha FROM code_e2e_marker")).all()
        if rows != [(context.run_id, context.frozen["source_sha256"])]:
            raise RuntimeError("QA database marker belongs to another run or source freeze")


def fixture_state(context):
    return json.loads((context.state / "fixture.json").read_text())


def set_fixture(context, layout):
    if layout not in LAYOUTS:
        raise RuntimeError("Unknown fixture layout")
    value = {
        "layout": layout,
        "generation_allowed": False,
        "company": "QA代码维护闭环-" + context.run_id[:8],
        "company_id": "qa-brand-" + context.run_id[:8],
        "job_id": "qa-job-" + context.run_id[:8],
    }
    save_json(context.state / "fixture.json", value)


class FixtureGateway:
    def __init__(self, context):
        self.context = context

    def complete(self, messages, *, purpose, task_id=None, **kwargs):
        from app.llm import Completion

        if purpose != "adapter_code_upgrade":
            raise RuntimeError("The QA model only serves code-maintenance generation")
        deadline = time.monotonic() + 90
        while not fixture_state(self.context).get("generation_allowed"):
            if time.monotonic() > deadline:
                raise RuntimeError("QA model barrier was not released; no candidate was generated")
            time.sleep(0.1)
        current = fixture_state(self.context)["layout"]
        if current == "old_program":
            payload = {
                "edits": [{"path": "adapter.py", "old": OLD_ANCHOR, "new": OLD_PATCH}],
                "dependencies": ["beautifulsoup4==4.13.4"],
            }
        elif current == "new_program":
            payload = {
                "edits": [{"path": "adapter.py", "old": OLD_PATCH, "new": NEW_PATCH}],
                "dependencies": ["beautifulsoup4==4.13.5"],
            }
        elif current == "bad_candidate":
            payload = {
                "edits": [
                    {
                        "path": "adapter.py",
                        "old": 'raise ValueError("No unique verified job description region")',
                        "new": 'raise RuntimeError("QA deliberately incompatible candidate")',
                    }
                ]
            }
        else:
            raise RuntimeError("No model generation is permitted for the native baseline")
        log = self.context.state / "mock-model.jsonl"
        calls = len(log.read_text().splitlines()) if log.exists() else 0
        if calls >= 4:
            raise RuntimeError("The whole QA scenario exceeded its four Mock-call ceiling")
        append_event(
            log,
            {
                "purpose": purpose,
                "task_id": str(task_id),
                "layout": current,
                "messages_sha256": digest(messages),
                "candidate_patch_sha256": digest(payload),
                "real_network_calls": 0,
            },
        )
        return Completion(
            content=json.dumps(payload, ensure_ascii=False),
            model="qa-code-mock",
            call_id=uuid.uuid4(),
            usage={},
            finish_reason="stop",
        )


def install_website_fixture(context):
    """Real Chromium and product route guard; only DNS/HTTP transport is replaced."""
    from playwright.sync_api import BrowserContext
    import app.platform_collectors as collectors
    from app.collection import CollectionError

    original_route = BrowserContext.route
    trace = context.state / "website-transport.jsonl"

    def allowed(url):
        parsed = urlsplit(url)
        item = fixture_state(context)
        return (
            parsed.scheme == "https"
            and parsed.hostname == HOST
            and parsed.path
            in {LIST_PATH, JSON_PATH, "/favicon.ico", "/job_detail/" + item["job_id"] + ".html"}
        )

    def fixture_dns(url):
        if not allowed(url):
            raise CollectionError(
                "qa_external_request_blocked",
                "QA transport blocks every nonfixture website request",
            )
        return "203.0.113.10"  # Documentation address only; no socket connects to it.

    collectors.validate_fetch_url = fixture_dns

    def bind(self, pattern, handler, *, times=None):
        def intercepted(route):
            class ProxyRoute:
                request = route.request

                def abort(self, *args, **kwargs):
                    append_event(
                        trace, {"action": "blocked", "path": urlsplit(route.request.url).path}
                    )
                    return route.abort(*args, **kwargs)

                def continue_(self, **kwargs):
                    if kwargs or not allowed(route.request.url):
                        return self.abort()
                    item = fixture_state(context)
                    path = urlsplit(route.request.url).path
                    append_event(
                        trace,
                        {
                            "action": "fixture_fulfilled",
                            "path": path,
                            "layout": item["layout"],
                            "external_network": False,
                        },
                    )
                    if path == JSON_PATH:
                        body = {
                            "zpData": {
                                "jobList": [
                                    {
                                        "encryptJobId": item["job_id"],
                                        "jobName": "QA平台工程师",
                                        "brandName": item["company"],
                                        "encryptBrandId": item["company_id"],
                                        "cityName": "北京",
                                        "salaryDesc": "20-30K",
                                    }
                                ]
                            }
                        }
                        return route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(body, ensure_ascii=False),
                        )
                    if path == "/favicon.ico":
                        return route.fulfill(status=204, body="")
                    if path == LIST_PATH:
                        html = (
                            "<html><body><h1>"
                            + item["company"]
                            + " QA招聘页</h1><p>平台工程师，仅用于受控工程验证。</p><script>fetch('"
                            + JSON_PATH
                            + "').then(r=>r.json()).then(x=>window.qaRows=x)</script></body></html>"
                        )
                    else:
                        html = (
                            '<html><body><h1>QA平台工程师</h1><div class="'
                            + LAYOUTS[item["layout"]]
                            + '">'
                            + DESCRIPTION
                            + " 这是"
                            + item["layout"]
                            + "受控样本。</div></body></html>"
                        )
                    return route.fulfill(
                        status=200, content_type="text/html; charset=utf-8", body=html
                    )

            return handler(ProxyRoute())

        return original_route(self, pattern, intercepted, times=times)

    BrowserContext.route = bind


def serve_api(context):
    import uvicorn

    database_guard(context)
    install_website_fixture(context)
    from app.main import app

    @app.middleware("http")
    async def internal_trace(request, call_next):
        payload = (
            await request.json() if request.url.path == "/api/internal/browser/collect" else None
        )
        response = await call_next(request)
        if payload is not None:
            append_event(
                context.state / "internal-http.jsonl",
                {
                    "task_id": payload.get("task_id"),
                    "expected_attempt": payload.get("expected_attempt"),
                    "status": response.status_code,
                },
            )
        return response

    uvicorn.run(app, host="127.0.0.1", port=18091, log_level="warning")


def serve_worker(context):
    database_guard(context)
    import app.adapter_code_service as service

    service.ModelGateway = lambda: FixtureGateway(context)
    from app.queue import celery

    celery.worker_main(
        [
            "worker",
            "--pool=solo",
            "--concurrency=1",
            "-Q",
            "celery,recruitment,maintenance",
            "--hostname",
            "code-e2e@%h",
            "--loglevel=WARNING",
        ]
    )


class Coordinator:
    def __init__(self, context):
        import httpx

        self.context = context
        self.path = context.state / "state.json"
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "run_id": context.run_id,
                "source_sha256": context.frozen["source_sha256"],
                "status": "running",
                "actions": {},
                "stages": {},
            }
        )
        if (
            self.state["run_id"] != context.run_id
            or self.state["source_sha256"] != context.frozen["source_sha256"]
        ):
            raise RuntimeError("Saved QA progress belongs to a different frozen scenario")
        self.client = httpx.Client(
            base_url="http://127.0.0.1:18091/api/v1", trust_env=False, timeout=15
        )
        self.save()

    def save(self):
        save_json(self.path, self.state)

    def api(self, method, path, body=None, key=None):
        response = self.client.request(
            method, path, json=body, headers={"Idempotency-Key": key} if key else None
        )
        if response.status_code not in {200, 201, 202}:
            raise RuntimeError(
                "QA API rejected " + method + " " + path + " with " + str(response.status_code)
            )
        return response.json()

    def tick(self):
        from app.queue import celery

        celery.send_task("app.dispatch_pending", queue="celery")
        celery.send_task("app.dispatch_maintenance", queue="maintenance")

    def stopped(self, label, value):
        details = value.get("details") or {}
        failure = {
            "label": label,
            "id": value.get("id"),
            "status": value.get("status"),
            "error_code": value.get("error_code")
            or details.get("code_error")
            or details.get("code_recovery_error")
            or details.get("last_error"),
        }
        self.state["wait_failure"] = failure
        self.save()
        raise QAWaitStopped("QA stopped before " + label + ": " + canonical(failure).decode())

    def wait(self, label, read, accept, seconds=180):
        import httpx

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.tick()
            try:
                value = read()
            except (httpx.ConnectError, httpx.ReadTimeout):
                value = None
            if value is not None and accept(value):
                return value
            if isinstance(value, dict) and value.get("status") in STOPPED_STATUSES:
                self.stopped(label, value)
            time.sleep(2)
        self.state["pending_wait"] = label
        self.save()
        raise RuntimeError(
            "Original QA work is incomplete; inspect saved IDs rather than submitting another version"
        )

    def seed(self):
        from sqlalchemy import select
        from app.db import session_factory
        from app.models import Company, SourceAccount, SourceBinding, Subject
        from app.browser_sessions import save_state

        ids = {
            name: uuid.uuid5(uuid.UUID(self.context.run_id), name)
            for name in ["subject", "company", "account", "binding"]
        }
        item = fixture_state(self.context)
        with session_factory()() as db:
            if not db.get(Company, ids["company"]):
                db.add(Subject(id=ids["subject"], name=item["company"]))
                db.flush()
                db.add(
                    Company(
                        id=ids["company"],
                        subject_id=ids["subject"],
                        name=item["company"],
                        role="other",
                        notes="Explicit QA fixture; no real platform login",
                    )
                )
                db.flush()
                db.add(
                    SourceAccount(
                        id=ids["account"],
                        kind="boss",
                        name="QA empty synthetic browser state",
                        health="saved",
                    )
                )
                db.flush()
                db.add(
                    SourceBinding(
                        id=ids["binding"],
                        company_id=ids["company"],
                        account_id=ids["account"],
                        kind="boss",
                        entry_url="https://" + HOST + LIST_PATH,
                        external_company_id=item["company_id"],
                    )
                )
                db.commit()
            if (
                db.scalar(select(Company.name).where(Company.id == ids["company"]))
                != item["company"]
            ):
                raise RuntimeError("Fixture identity conflict")
        save_state(ids["account"], {"cookies": [], "origins": []})
        self.state["ids"] = {name: str(value) for name, value in ids.items()}
        self.save()

    def formal(self):
        from sqlalchemy import text
        from app.db import session_factory

        result = {}
        with session_factory()() as db:
            for table in FORMAL_TABLES:
                rows = (
                    db.execute(text('SELECT * FROM "' + table + '" ORDER BY id')).mappings().all()
                )
                result[table] = {"count": len(rows), "sha256": digest([dict(row) for row in rows])}
        return result

    def registry(self):
        return next(
            item
            for item in self.api("GET", "/adapter-maintenance/adapters")
            if item["kind"] == "boss"
        )

    def flags(self, auto_publish):
        return self.api(
            "PATCH",
            "/adapter-maintenance/adapters/boss/settings",
            {
                "agent_enabled": False,
                "auto_repair_enabled": False,
                "code_agent_enabled": True,
                "code_auto_publish_enabled": auto_publish,
                "code_upstream_enabled": False,
            },
        )

    def start_collection(self, name):
        from sqlalchemy import select
        from app.db import session_factory
        from app.models import RunBatch, TaskRun

        key = "code-e2e:" + self.context.run_id + ":" + name
        if name not in self.state["actions"]:
            self.state["actions"][name] = {"key": key, "submission_attempted": True}
            self.save()
            registered = self.api(
                "POST",
                "/recruitment/runs",
                {
                    "company_ids": [self.state["ids"]["company"]],
                    "name": "QA " + name,
                    "filters": {"sources": ["boss"], "max_pages": 2, "max_details": 2},
                    "idempotency_key": key,
                },
                key,
            )
            self.state["actions"][name]["batch_id"] = registered["batch_id"]
            self.save()
        with session_factory()() as db:
            batch = db.scalar(select(RunBatch).where(RunBatch.idempotency_key == key))
            if batch is None:
                raise RuntimeError("Prior QA submission is unknown; do not blindly re-submit")
            task = db.scalar(select(TaskRun).where(TaskRun.batch_id == batch.id))
            self.state["actions"][name]["task_id"] = str(task.id)
            self.save()
            return str(task.id)

    def task(self, task_id):
        return self.api("GET", "/tasks/" + task_id)

    def incident(self, original):
        from sqlalchemy import select
        from app.auto_maintenance_models import AdapterRepairIncident
        from app.db import session_factory
        from app.serialization import row

        with session_factory()() as db:
            found = db.scalar(
                select(AdapterRepairIncident).where(
                    AdapterRepairIncident.task_id == uuid.UUID(original)
                )
            )
            return row(found) if found else None

    def proposal(self, incident):
        from sqlalchemy import select
        from app.adapter_code_models import AdapterCodeProposal
        from app.db import session_factory

        if incident is None:
            return None
        identifier = incident.get("details", {}).get("code_proposal_id")
        with session_factory()() as db:
            value = (
                db.get(AdapterCodeProposal, uuid.UUID(identifier))
                if identifier
                else db.scalar(
                    select(AdapterCodeProposal).where(
                        AdapterCodeProposal.task_id == uuid.UUID(incident["maintenance_task_id"])
                    )
                )
                if incident.get("maintenance_task_id")
                else None
            )
            if value:
                return self.api("GET", "/adapter-maintenance/code-proposals/" + str(value.id))
            if incident.get("status") in STOPPED_STATUSES:
                self.stopped("proposal-creation", incident)
            return None

    def prove_execution(self, task_id, release, expected_dependency):
        from sqlalchemy import func, select
        from app.adapter_code_models import AdapterCodePackage
        from app.adapter_runtime_models import AdapterProgramExecution
        from app.adapter_runner_client import AdapterRunnerClient
        from app.db import session_factory
        from app.models import JobObservation, JobSourceRecord, SourceRun

        reference = release["manifest"]["program"]
        with session_factory()() as db:
            source = db.scalar(select(SourceRun).where(SourceRun.task_id == uuid.UUID(task_id)))
            proof = source.counts.get("code_execution", {})
            if (
                proof.get("verified") is not True
                or proof.get("release_id") != release["id"]
                or proof.get("fingerprint") != release["fingerprint"]
                or proof.get("bundle_sha256") != reference["bundle_sha256"]
                or proof.get("lock_sha256") != reference["lock_sha256"]
                or proof.get("runtime_sha256") != reference["runtime_sha256"]
                or not proof.get("run_ids")
            ):
                raise RuntimeError("The real SourceRun lacks verified program execution evidence")
            package = db.scalar(
                select(AdapterCodePackage).where(
                    AdapterCodePackage.bundle_sha == reference["bundle_sha256"]
                )
            )
            assert expected_dependency in package.dependencies
            executions = db.scalars(
                select(AdapterProgramExecution).where(
                    AdapterProgramExecution.task_id == uuid.UUID(task_id)
                )
            ).all()
            accepted = [item for item in executions if item.status == "accepted"]
            assert accepted
            assert source.status in {"succeeded", "partial"}
            observations = db.scalar(
                select(func.count())
                .select_from(JobObservation)
                .where(JobObservation.source_run_id == source.id)
            )
            assert observations > 0
            stored = db.scalar(
                select(JobSourceRecord).where(JobSourceRecord.binding_id == source.binding_id)
            )
            assert fixture_state(self.context)["layout"] in stored.data["description"]
            client = AdapterRunnerClient()
            for item in accepted:
                client.verify_result(
                    item.result,
                    request=item.request,
                    lock_sha256=reference["lock_sha256"],
                    expected_runtime=reference["runtime_sha256"],
                )
                assert (
                    item.result["receipt"]["dependency_environment_sha256"]
                    == reference["dependency_environment_sha256"]
                )
                assert item.remote_run_id in proof["run_ids"]
            return {
                "task_id": task_id,
                "source_run_id": str(source.id),
                "code_execution": proof,
                "accepted_run_ids": [item.remote_run_id for item in accepted],
                "dependencies": package.dependencies,
                "persisted_observations": observations,
            }

    def prove_rejected_candidates(self, proposal):
        from sqlalchemy import select
        from app.adapter_code_models import AdapterCodePackage
        from app.adapter_code_service import validation_summary
        from app.adapter_runner_client import AdapterRunnerClient
        from app.collection import CollectionError
        from app.db import session_factory

        if (
            proposal["status"] != "failed"
            or proposal["error_code"] not in {"code_oracle_failed", "candidate_budget"}
            or proposal["attempts"] != proposal["max_candidates"]
            or proposal["max_candidates"] != 2
        ):
            self.stopped("bad-candidate-rejection", proposal)
        failures = [
            item for item in proposal["candidate_history"] if item["status"] == "validation_failed"
        ]
        assert len(failures) == 2 and {item["attempt"] for item in failures} == {1, 2}
        cases = proposal["evidence_cases"]
        failure_ids = {case["id"] for case in cases if case.get("role") == "failure"}
        assert failure_ids
        client = AdapterRunnerClient()
        proof = []
        with session_factory()() as db:
            for attempt in failures:
                bundle_sha = attempt["bundle_sha256"]
                package = db.scalar(
                    select(AdapterCodePackage).where(AdapterCodePackage.bundle_sha == bundle_sha)
                )
                assert package is not None
                refs = attempt["validation_runs"]
                classifications = []
                for ref in refs:
                    receipt = ref["receipt"]
                    assert ref["run_id"] == receipt["run_id"]
                    assert ref["request"]["bundle_sha256"] == bundle_sha
                    classification = None
                    try:
                        output = client.verify_result(
                            receipt,
                            request=ref["request"],
                            lock_sha256=package.lock_sha,
                            expected_runtime=self.context.frozen["runtime_sha256"],
                        )
                    except CollectionError as exc:
                        if exc.code != "runner_execution_failed":
                            raise
                        output, classification = None, exc.code
                    assert output == ref.get("output")
                    assert classification == ref.get("verification_outcome")
                    classifications.append({"run_id": ref["run_id"], "outcome": classification or "succeeded"})
                report = validation_summary(cases, refs, bundle_sha=bundle_sha)
                assert report == attempt["report"] and report["passed"] is False
                assert any(
                    item["case_id"] in failure_ids and item["passed"] is False
                    for item in report["checks"]
                )
                assert all(
                    item["passed"] is True
                    for item in report["checks"]
                    if item["case_id"] not in failure_ids
                )
                proof.append({"attempt": attempt["attempt"], "bundle_sha256": bundle_sha,
                              "report_sha256": digest(report), "signed_runs": classifications})
        return proof

    def repair_stage(self, layout, dependency, *, good):
        saved = self.state["stages"].get(layout)
        if saved and saved.get("complete"):
            return saved
        set_fixture(self.context, layout)
        self.flags(False)
        before = self.registry()
        original = self.start_collection(layout)
        self.wait(
            layout + ":original",
            lambda: self.task(original),
            lambda item: item["status"] in {"failed", "partial"},
        )
        incident = self.wait(
            layout + ":incident", lambda: self.incident(original), lambda item: item is not None
        )
        baseline = self.formal()
        current_fixture = fixture_state(self.context)
        current_fixture["generation_allowed"] = True
        save_json(self.context.state / "fixture.json", current_fixture)
        proposal = self.wait(
            layout + ":proposal",
            lambda: self.proposal(self.incident(original)),
            lambda item: item is not None,
        )
        proposal_id = proposal["id"]
        proposal = self.wait(
            layout + ":validation",
            lambda: self.api("GET", "/adapter-maintenance/code-proposals/" + proposal_id),
            lambda item: (
                item["status"]
                in ({"validated", "awaiting_publish"} | ({"failed"} if not good else set()))
            ),
        )
        if self.formal() != baseline:
            raise RuntimeError("Shadow validation changed formal business records")
        result = {
            "original_task_id": original,
            "incident_id": incident["id"],
            "proposal_id": proposal_id,
            "shadow_unchanged": True,
            "before_release_id": before["registry"]["active_release_id"],
            "before_generation": before["registry"]["release_generation"],
        }
        if not good:
            result["rejected_candidates"] = self.prove_rejected_candidates(proposal)
            current = self.registry()
            assert (
                current["registry"]["active_release_id"] == result["before_release_id"]
                and current["registry"]["release_generation"] == result["before_generation"]
            )
            assert not self.incident(original).get("recovery_task_id")
            result.update(complete=True, old_release_preserved=True)
        else:
            assert proposal["status"] in {"validated", "awaiting_publish"}
            self.flags(True)
            recovered = self.wait(
                layout + ":recovery",
                lambda: self.incident(original),
                lambda item: item.get("status") == "recovered",
            )
            assert recovered["status"] == "recovered" and recovered["recovery_attempts"] == 1
            recovery_id = recovered["recovery_task_id"]
            recovery = self.task(recovery_id)
            assert recovery_id != original and recovery["status"] in {"succeeded", "partial"}
            current = self.registry()
            assert current["registry"]["release_generation"] == result["before_generation"] + 1
            result.update(
                complete=True,
                release=current["active"],
                recovery=self.prove_execution(recovery_id, current["active"], dependency),
                formal_after=self.formal(),
            )
            assert (
                result["formal_after"]["job_observations"]["count"]
                > baseline["job_observations"]["count"]
            )
        self.state["stages"][layout] = result
        self.save()
        return result

    def run(self):
        self.wait(
            "api-ready",
            lambda: self.api("GET", "/readiness"),
            lambda item: item["status"] == "ready",
            seconds=30,
        )
        self.api(
            "POST",
            "/auth/login",
            {"username": os.environ["ADMIN_USERNAME"], "password": os.environ["ADMIN_PASSWORD"]},
        )
        self.seed()
        self.api("POST", "/adapter-maintenance/register", {})
        old = self.repair_stage("old_program", "beautifulsoup4==4.13.4", good=True)
        new = self.repair_stage("new_program", "beautifulsoup4==4.13.5", good=True)
        self.repair_stage("bad_candidate", None, good=False)
        assert (
            old["release"]["manifest"]["program"]["bundle_sha256"]
            != new["release"]["manifest"]["program"]["bundle_sha256"]
        )
        assert (
            old["release"]["manifest"]["program"]["lock_sha256"]
            != new["release"]["manifest"]["program"]["lock_sha256"]
        )
        if not self.state.get("rollback_complete"):
            self.flags(False)
            current = self.registry()
            if current["registry"]["active_release_id"] != old["release"]["id"]:
                assert current["registry"]["active_release_id"] == new["release"]["id"]
                self.state["rollback_intent"] = {
                    "expected_active_release_id": current["registry"]["active_release_id"],
                    "expected_generation": current["registry"]["release_generation"],
                }
                self.save()
                self.api(
                    "POST",
                    "/adapter-maintenance/releases/" + old["release"]["id"] + "/rollback",
                    self.state["rollback_intent"],
                )
            set_fixture(self.context, "old_program")
            task_id = self.start_collection("after-rollback")
            self.wait(
                "rollback-collection",
                lambda: self.task(task_id),
                lambda item: item["status"] in {"succeeded", "partial", "failed"},
            )
            self.state["rollback_execution"] = self.prove_execution(
                task_id, old["release"], "beautifulsoup4==4.13.4"
            )
            self.state["rollback_complete"] = True
            self.save()
        from sqlalchemy import func, select
        from app.db import session_factory
        from app.models import ModelCall
        from app.notification_models import NotificationDelivery

        with session_factory()() as db:
            assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
            assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
        calls = [
            json.loads(line)
            for line in (self.context.state / "internal-http.jsonl").read_text().splitlines()
        ]
        assert len(calls) >= 5 and all(item["expected_attempt"] >= 1 for item in calls)
        self.state.update(
            status="passed",
            real_llm_calls=0,
            real_notifications=0,
            real_platform_logins=0,
            internal_http_calls=len(calls),
            candidate_execution_on_driver=False,
        )
        self.save()
        save_json(self.context.state / "summary.json", self.state)


@contextmanager
def processes(args, context):
    children = []
    try:
        for role in ["api", "worker"]:
            log = (context.state / (role + ".log")).open("ab")
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "--role",
                    role,
                    "--freeze-manifest",
                    str(args.freeze_manifest),
                    "--state-dir",
                    str(args.state_dir),
                ],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            children.append((child, log))
        save_json(
            context.state / "processes.json",
            {
                "supervisor_pid": os.getpid(),
                "api_pid": children[0][0].pid,
                "worker_pid": children[1][0].pid,
            },
        )
        yield
    finally:
        for child, log in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
            log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--role", choices=["prepare", "supervise", "api", "worker"], default="prepare"
    )
    parser.add_argument(
        "--freeze",
        action="store_true",
        help="Use only after the root confirms the collector/hooks source freeze",
    )
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("/qa-state"))
    parser.add_argument("--runtime-digest", default="")
    args = parser.parse_args()
    if args.role == "prepare":
        prepare(args)
        return
    context = guard(args)
    if args.role == "api":
        serve_api(context)
        return
    if args.role == "worker":
        serve_worker(context)
        return
    database_guard(context, create_marker=True)
    if not (context.state / "fixture.json").exists():
        set_fixture(context, "native")
    with processes(args, context):
        coordinator = Coordinator(context)
        try:
            coordinator.run()
        except Exception:
            coordinator.state["status"] = "incomplete"
            coordinator.save()
            raise
    print(
        json.dumps(
            {"status": "passed", "real_llm_calls": 0, "candidate_execution_on_driver": False}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "not_passed",
                    "error_type": type(error).__name__,
                    "error_code": getattr(error, "code", "inspect_saved_evidence"),
                    "executed_half_finished_sources": False,
                }
            )
        )
        raise SystemExit(1) from None
