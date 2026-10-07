"""Frozen-source FastAPI -> real SearXNG -> isolated PostgreSQL acceptance.

The API uses its real transport. A QA egress guard rejects every HTTP destination
except one exact managed search request; it never manufactures a response.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_code_maintenance_e2e import append_event, save_json, source_manifest  # noqa: E402

ENDPOINT = "http://recruitment-search:8080/search"
QUERY = '"金蝶" 招聘 官网'


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(args):
    manifest = source_manifest()
    save_json(args.freeze_manifest, {**manifest, "frozen": args.freeze,
        "driver_sha256": file_sha(Path(__file__)),
        "helper_sha256": file_sha(ROOT / "scripts/verify_code_maintenance_e2e.py"),
        "real_search_requests": 1, "llm_calls": 0, "main_database_writes": 0})
    print(json.dumps({"frozen": args.freeze, "source_sha256": manifest["source_sha256"], "executed": False}))


def guard(args):
    if sys.platform != "linux" or (ROOT / ".env").exists():
        raise RuntimeError("Only clean remote Linux QA staging is allowed")
    run_id = os.environ.get("HR_SEARCH_E2E_RUN_ID", "")
    database = urlsplit(os.environ.get("DATABASE_URL", ""))
    if (not re.fullmatch(r"[a-f0-9]{32}", run_id) or database.hostname != "search-e2e-postgres"
            or database.path != "/hr_search_qa_" + run_id[:12]):
        raise RuntimeError("A dedicated search QA run/database is required")
    frozen = json.loads(args.freeze_manifest.read_text())
    if (frozen.get("frozen") is not True
            or frozen["source_sha256"] != source_manifest()["source_sha256"]
            or frozen["driver_sha256"] != file_sha(Path(__file__))
            or frozen["helper_sha256"] != file_sha(ROOT / "scripts/verify_code_maintenance_e2e.py")):
        raise RuntimeError("QA sources no longer match the approved freeze")
    state = args.state_dir.resolve()
    if run_id not in str(state) or not all(
        Path(os.environ.get(key, "/forbidden")).resolve().is_relative_to(state)
        for key in ["STORAGE_DIR", "BROWSER_PROFILES_DIR"]
    ):
        raise RuntimeError("QA artifacts must stay inside the dedicated run directory")
    if (os.environ.get("LLM_API_KEY") or os.environ.get("LLM_DAILY_REQUEST_LIMIT") != "0"
            or os.environ.get("SMTP_HOST") or os.environ.get("DISCOVERY_INTERNAL_SEARCH_URL") != ENDPOINT
            or os.environ.get("DISCOVERY_SEARCH_TIMEOUT_SECONDS") != "15"):
        raise RuntimeError("QA search/budget boundary does not match")
    state.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "backend"))
    return SimpleNamespace(run_id=run_id, state=state, frozen=frozen, database=database.path[1:])


def database_guard(context, *, create=False):
    from sqlalchemy import func, select, text
    from app.db import session_factory
    from app.models import Company

    with session_factory()() as db:
        assert db.scalar(text("SELECT current_database()")) == context.database
        if create:
            db.execute(text("CREATE TABLE IF NOT EXISTS search_e2e_marker (run_id text PRIMARY KEY, source_sha text NOT NULL)"))
            rows = db.execute(text("SELECT run_id,source_sha FROM search_e2e_marker")).all()
            if not rows:
                assert db.scalar(select(func.count()).select_from(Company)) == 0
                db.execute(text("INSERT INTO search_e2e_marker VALUES (:id,:sha)"),
                           {"id": context.run_id, "sha": context.frozen["source_sha256"]})
                db.commit()
        assert db.execute(text("SELECT run_id,source_sha FROM search_e2e_marker")).all() == [
            (context.run_id, context.frozen["source_sha256"])]


def serve_api(context):
    import httpx
    import uvicorn

    database_guard(context)
    trace = context.state / "http-egress.jsonl"
    lock = threading.Lock()
    original_send = httpx.Client.send

    def actual_send(client, request, *args, **kwargs):
        with lock:
            previous = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
            attempts = sum(item["event"] == "request" for item in previous)
            allowed = (request.method == "GET" and str(request.url.copy_with(query=None)) == ENDPOINT
                       and dict(request.url.params) == {"q": QUERY, "format": "json", "language": "zh-CN"}
                       and attempts == 0)
            append_event(trace, {"event": "request", "method": request.method, "host": request.url.host,
                                "path": request.url.path, "allowed": allowed})
            if not allowed:
                raise RuntimeError("QA blocks candidate visits, model calls and duplicate search requests")
        response = original_send(client, request, *args, **kwargs)
        append_event(trace, {"event": "response", "status": response.status_code})
        return response

    httpx.Client.send = actual_send
    from app.main import app
    uvicorn.run(app, host="127.0.0.1", port=18092, log_level="warning")


def run_api_checks(context):
    import httpx
    from sqlalchemy import func, select
    from app.db import session_factory
    from app.models import AuditEvent, Company, ModelCall, SourceBinding, TaskRun
    from app.recruitment_management_models import RecruitmentEntryCandidate

    state_path = context.state / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "run_id": context.run_id, "source_sha256": context.frozen["source_sha256"], "status": "running"}
    assert state["run_id"] == context.run_id and state["source_sha256"] == context.frozen["source_sha256"]
    if state.get("discovery_submission_attempted"):
        raise RuntimeError("The original discovery was already attempted; inspect saved evidence, never resubmit")
    save_json(state_path, state)
    notes = "QA search E2E only; not a real watch target; run=" + context.run_id
    with httpx.Client(base_url="http://127.0.0.1:18092/api/v1", trust_env=False, timeout=20) as client:
        def request(method, path, body=None, expected=200):
            response = client.request(method, path, json=body)
            assert response.status_code == expected, (method, path, response.status_code)
            return response.json()

        deadline = time.monotonic() + 30
        while True:
            try:
                response = client.get("/readiness")
                if response.status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Dedicated QA API did not become ready")
            time.sleep(0.2)
        request("POST", "/auth/login", {"username": os.environ["ADMIN_USERNAME"], "password": os.environ["ADMIN_PASSWORD"]})
        configuration = request("GET", "/settings/recruitment-discovery")
        assert configuration["configured"] and configuration["config"]["origin"] == "deployment"
        assert configuration["config"]["endpoint_url"] == ENDPOINT
        with session_factory()() as db:
            existing = db.scalars(select(Company).where(Company.notes == notes)).all()
            assert len(existing) <= 1
            company_id = str(existing[0].id) if existing else None
        if company_id is None:
            if state.get("company_submission_attempted"):
                raise RuntimeError("Prior company creation outcome is unknown; no duplicate submission")
            state["company_submission_attempted"] = True
            save_json(state_path, state)
            company = request("POST", "/companies", {"name": "金蝶", "role": "other", "notes": notes}, expected=201)
            company_id = company["id"]
        state.update(company_id=company_id, discovery_submission_attempted=True)
        save_json(state_path, state)
        response = request("POST", "/companies/" + company_id + "/entry-discovery", {"max_candidates": 5})
        save_json(context.state / "discovery-response.json", response)
        assert response["status"] == "candidates_found" and 1 <= len(response["candidates"]) <= 5
        assert response["search"]["query"] == QUERY and response["search"]["provider"] == "searxng"
        assert response["search"]["source_url"] == ENDPOINT
        assert re.fullmatch(r"[a-f0-9]{64}", response["search"]["response_text_sha256"])
        persisted = request("GET", "/companies/" + company_id + "/entry-candidates")
        assert {item["id"] for item in persisted} == {item["id"] for item in response["candidates"]}
        assert any(item["url"].startswith("https://www.kingdee.com/job") for item in persisted)
        with session_factory()() as db:
            company = db.get(Company, uuid.UUID(company_id))
            assert company.role == "other" and company.website is None and company.notes == notes
            candidates = db.scalars(select(RecruitmentEntryCandidate).where(RecruitmentEntryCandidate.company_id == company.id)).all()
            assert len(candidates) == len(persisted)
            assert all(item.status == "pending" and item.bound_source_id is None and item.evidence["query"] == QUERY for item in candidates)
            audits = db.scalars(select(AuditEvent).where(AuditEvent.action == "recruitment.entries_discovered",
                                                       AuditEvent.target == company_id)).all()
            assert len(audits) == 1 and audits[0].detail["search"] == response["search"]
            counts = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                      for model in [Company, RecruitmentEntryCandidate, SourceBinding, ModelCall, TaskRun]}
            assert counts["companies"] == 1 and counts["source_bindings"] == counts["model_calls"] == counts["task_runs"] == 0
            state.update(qa_counts=counts, discovery_audit_id=str(audits[0].id), search_evidence=response["search"])
        trace = [json.loads(line) for line in (context.state / "http-egress.jsonl").read_text().splitlines()]
        requests = [item for item in trace if item["event"] == "request"]
        responses = [item for item in trace if item["event"] == "response"]
        assert len(requests) == len(responses) == 1 and requests[0]["allowed"] and responses[0]["status"] == 200
        request("POST", "/auth/logout", {})
    assert source_manifest()["source_sha256"] == context.frozen["source_sha256"]
    state.update(status="passed", real_search_requests=1, candidate_site_requests=0, real_llm_calls=0,
                 main_database_writes=0, source_bindings_created=0, real_platform_logins=0)
    save_json(state_path, state)
    save_json(context.state / "summary.json", state)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["prepare", "api", "supervise"], default="prepare")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--freeze-manifest", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("/qa-state"))
    args = parser.parse_args()
    if args.role == "prepare":
        prepare(args)
        return
    context = guard(args)
    if args.role == "api":
        serve_api(context)
        return
    database_guard(context, create=True)
    with (context.state / "api.log").open("ab") as log:
        child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--role", "api",
            "--freeze-manifest", str(args.freeze_manifest), "--state-dir", str(args.state_dir)],
            stdout=log, stderr=log, start_new_session=True)
        try:
            run_api_checks(context)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
    print(json.dumps({"status": "passed", "real_search_requests": 1, "llm_calls": 0, "main_database_writes": 0}))


if __name__ == "__main__":
    main()
