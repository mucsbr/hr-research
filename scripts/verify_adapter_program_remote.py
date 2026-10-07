"""Authorized Linux-only Broker/client/first-party integration; no local candidate import."""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-env", type=Path, required=True)
    parser.add_argument("--runtime-digest", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:18082")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        raise RuntimeError("This verification only runs on the authorized remote Linux node")
    if (ROOT / ".env").exists():
        raise RuntimeError("Verification staging must not contain the development .env")
    if stat.S_IMODE(args.runner_env.stat().st_mode) & 0o077:
        raise RuntimeError("Runner configuration must be private")
    values = dict(line.split("=", 1) for line in args.runner_env.read_text().splitlines() if "=" in line)
    token = values["RUNNER_TOKEN"]
    if len(token) < 32:
        raise RuntimeError("Runner token is not configured")
    sys.path.insert(0, str(ROOT / "backend"))
    from pydantic import SecretStr
    from app.adapter_code_package import build_bundle, canonical_bytes, content_sha256, validate_candidate_spec
    from app.adapter_program import prepare_program_input, validate_detail_output, validate_list_output, validate_navigation
    from app.adapter_runner_client import AdapterRunnerClient
    from app.collection import CollectionError, JobDocument

    settings = SimpleNamespace(app_env="development", adapter_runner_url=args.url,
                               adapter_runner_token=SecretStr(token), adapter_runner_runtime_digest=args.runtime_digest,
                               adapter_runner_timeout_seconds=45)
    client = AdapterRunnerClient(settings)
    health = client.health()
    cases_document = json.loads((ROOT / "adapters/code/qa_cases_v1.json").read_text())
    cases = cases_document["cases"]
    sources = {name: (ROOT / "adapters/code" / file).read_text() for name, file in {
        "old": "boss_reference_v1.py", "new": "boss_reference_v2.py"}.items()}
    dependencies = {"old": [], "new": ["beautifulsoup4==4.13.5"], "missing_dependency": []}
    plan_hash = content_sha256({"cases": cases_document, "sources": sources, "dependencies": dependencies,
                               "runtime": args.runtime_digest})
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = args.output / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "plan_sha256": plan_hash, "prefix": "qa-program:" + plan_hash[:20],
        "runtime": health["runtime_sha256"], "locks": {}, "bundles": {}, "runs": {}, "checks": []}
    if state["plan_sha256"] != plan_hash:
        raise RuntimeError("Saved verification belongs to another frozen plan; do not overwrite it")

    def save():
        temporary = state_path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(state_path)

    save()
    contexts = {}
    for case in cases:
        material = deepcopy(cases_document["materials"][case["id"]])
        if material.get("job"):
            material["job"] = JobDocument.model_validate(material["job"])
        context = prepare_program_input(operation=case["operation"], **material)
        if context.request != case["input"]:
            raise RuntimeError("Trusted QA input changed; frozen oracle cannot be reused")
        contexts[case["id"]] = context

    def prepare_bundle(version):
        pins = dependencies[version]
        lock_key = content_sha256(pins)
        if lock_key not in state["locks"]:
            state["locks"][lock_key] = client.resolve_dependencies(pins)["lock"]
            save()
        source = sources["new" if version == "missing_dependency" else version]
        spec = validate_candidate_spec({"schema_version": 1, "contract": "python_adapter_v1", "kind": "boss",
                                        "files": {"adapter.py": source}, "dependencies": pins})
        bundle = build_bundle(spec, state["locks"][lock_key])
        uploaded = client.upload_bundle(bundle)
        state["bundles"][version] = {"bundle_sha256": uploaded["bundle_sha256"],
                                      "lock_sha256": uploaded["lock_sha256"], "dependencies": pins,
                                      "source_sha256": hashlib.sha256(source.encode()).hexdigest()}
        save()
        return state["bundles"][version]

    def run_case(version, case):
        bundle = state["bundles"][version]
        name = version + ":" + case["id"]
        key = state["prefix"] + ":" + name
        request = client.run_request(bundle["bundle_sha256"], case["operation"], case["input"], idempotency_key=key)
        fresh = name not in state["runs"]
        if fresh:
            # Persist before the potentially ambiguous submit. A restart only looks up this key.
            state["runs"][name] = {"request": request, "submission_attempted": True}
            save()
        saved = state["runs"][name]
        if saved["request"] != request:
            raise RuntimeError("Saved run and frozen request do not match")
        if "run_id" not in saved:
            if fresh:
                registered = client.submit_run(bundle["bundle_sha256"], case["operation"], case["input"], idempotency_key=key)
            else:
                registered = client.lookup_run(key)
                if registered is None:
                    raise RuntimeError("Prior submission is unknown; do not automatically submit again")
            if registered.get("request_sha256") != content_sha256(request):
                raise RuntimeError("Broker lookup returned another request")
            saved["run_id"] = registered["run_id"]
            save()
        if "result" not in saved:
            deadline = time.monotonic() + 55
            while time.monotonic() < deadline:
                result = client.get_run(saved["run_id"])
                if result["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                    saved["result"] = result
                    save()
                    break
                time.sleep(0.25)
            else:
                raise RuntimeError("Original run is still active; resume by lookup without resubmission")
        result = saved["result"]
        if result["status"] not in {"succeeded", "failed"}:
            raise RuntimeError("Unknown/cancelled execution is not proof of a controlled failure")
        try:
            parsed = client.verify_result(result, request=request, lock_sha256=bundle["lock_sha256"],
                                          expected_runtime=args.runtime_digest)
        except CollectionError as exc:
            # verify_result checks signature, all bindings and actual isolation before this code.
            if exc.code != "runner_execution_failed" or result["status"] != "failed":
                raise
            parsed = None
        summary = {"case_id": case["id"], "version": version, "operation": case["operation"],
                   "run_id": result["run_id"], "status": result["status"],
                   "verified_broker_receipt": True, "expected_matches": False, "first_party_accepted": False}
        if parsed is not None:
            summary["expected_matches"] = canonical_bytes(parsed) == canonical_bytes(case["expected"])
            context = contexts[case["id"]]
            try:
                if case["operation"] == "parse_list":
                    checked = validate_list_output(context, parsed)
                    summary["first_party_accepted"] = checked.recognized and len(checked.records) == 1
                    summary["verified_job_ids"] = [doc.source_job_id for doc in checked.records]
                elif case["operation"] == "parse_detail":
                    checked, _ = validate_detail_output(context, parsed)
                    summary["first_party_accepted"] = checked.description == "负责平台研发"
                else:
                    checked = validate_navigation(context, parsed)
                    summary["first_party_accepted"] = True
                    summary["verified_actions"] = [action.type for action in checked]
            except CollectionError:
                summary["first_party_accepted"] = False
        should_pass = version == "new" or (version == "old" and case["role"] == "regression")
        if should_pass:
            assert summary["expected_matches"] and summary["first_party_accepted"], summary
        elif version == "old":
            assert not summary["expected_matches"], summary
        else:
            assert result["status"] == "failed", "The identical new source unexpectedly ran without its dependency lock"
            receipt = result["receipt"]
            assert receipt.get("error_code") == "candidate_exit_nonzero" and receipt.get("oom_killed") is False
            assert "ModuleNotFoundError" in receipt.get("stderr_excerpt", "") and "'bs4'" in receipt["stderr_excerpt"]
            summary["missing_dependency_proven"] = "bs4"
        saved["summary"] = summary
        save()
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    for version in ("old", "new"):
        prepare_bundle(version)
        for case in cases:
            run_case(version, case)
    prepare_bundle("missing_dependency")
    assert state["bundles"]["new"]["source_sha256"] == state["bundles"]["missing_dependency"]["source_sha256"]
    assert state["bundles"]["new"]["lock_sha256"] != state["bundles"]["old"]["lock_sha256"]
    run_case("missing_dependency", next(case for case in cases if case["id"] == "moved-detail"))
    final = {"status": "passed", "scope": "Broker + real platform client + first-party program validators; no publication/recovery pipeline",
             "runtime_sha256": state["runtime"], "plan_sha256": plan_hash, "bundles": state["bundles"],
             "checks": [saved["summary"] for saved in state["runs"].values()], "candidate_executions_on_driver": 0,
             "real_llm_calls": 0, "real_platform_fetches": 0, "real_notifications": 0,
             "dependency_api_proof": "New source calls BeautifulSoup.select_one/get_text successfully; identical source with empty lock fails remotely"}
    (args.output / "summary.json").write_text(json.dumps(final, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": "passed", "checks": len(final["checks"]), "real_llm_calls": 0}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print private configuration, HTTP authorization headers, or arbitrary stderr.
        print(json.dumps({"status": "incomplete", "error_type": type(exc).__name__,
                          "error_code": getattr(exc, "code", "verification_failed")}, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None
