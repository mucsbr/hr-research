"""Bounded container smoke: local endpoints only, no LLM or external data request."""
import argparse
from http.cookiejar import CookieJar
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="deploy/server.env")
    parser.add_argument("--output", default="deploy/verification-results.json")
    args = parser.parse_args()
    values = dict(line.split("=", 1) for line in Path(args.env_file).read_text().splitlines()
                  if line and not line.startswith("#") and "=" in line)
    base = f"http://127.0.0.1:{int(values.get('HTTP_PORT', '18080'))}"
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    checks = []

    def request(method, path, data=None, expected=200, headers=None):
        body = json.dumps(data).encode() if data is not None else None
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        try:
            response = opener.open(Request(base + path, body, request_headers, method=method), timeout=15)
            code, raw = response.status, response.read()
        except HTTPError as exc:
            code, raw = exc.code, exc.read()
        assert code == expected, f"{method} {path}: expected {expected}, got {code}"
        if raw.startswith(b"{") or raw.startswith(b"["):
            return json.loads(raw)
        return raw

    request("GET", "/")
    request("GET", "/companies")  # SPA deep link.
    assert request("GET", "/api/v1/readiness")["status"] == "ready"
    request("GET", "/api/v1/companies", expected=401)
    for path in ["/api/internal/browser/collect", "/api/internal", "/api/%69nternal/browser/collect"]:
        request("POST", path, {}, expected=404)
    checks.extend(["frontend_and_spa", "readiness", "anonymous_denied", "internal_routes_hidden"])
    request("POST", "/api/v1/auth/login", {
        "username": values.get("ADMIN_USERNAME", "admin"), "password": values["ADMIN_PASSWORD"]})
    assert request("GET", "/api/v1/auth/me")["role"] == "admin"
    request("POST", "/api/v1/companies", {"name": "blocked-origin"}, expected=403,
            headers={"Origin": "https://invalid.example"})
    system = request("GET", "/api/v1/system/status")
    assert not system["llm"]["configured"]
    assert system["llm"]["daily_request_limit"] == 0
    assert system["llm"]["calls_today"] == 0
    checks.extend(["authenticated_login", "origin_guard", "llm_disabled_and_zero_calls"])
    company = request("POST", "/api/v1/companies", {
        "name": "容器验证样本-" + uuid.uuid4().hex[:8], "role": "other",
        "notes": "部署验证人工样本，不是用户竞品；仅验证无登录状态，不采集外部网站。",
    }, expected=201)
    request("POST", "/api/v1/source-bindings", {
        "company_id": company["id"], "kind": "boss", "adapter": "generic",
        "entry_url": "https://www.zhipin.com/gongsi/container-verification.html",
    }, expected=201)
    run = request("POST", "/api/v1/recruitment/runs", {
        "company_ids": [company["id"]], "name": "容器队列与未登录状态验证",
        "filters": {"sources": ["boss"], "max_pages": 1, "max_details": 1},
    }, expected=202)
    deadline = time.monotonic() + 65
    while True:
        batch = request("GET", "/api/v1/batches/" + run["batch_id"])
        if batch["tasks"] and all(task["status"] not in {"queued", "running"} for task in batch["tasks"]):
            break
        if time.monotonic() > deadline:
            raise RuntimeError("The existing batch did not complete within 65 seconds; inspect it, do not recreate it.")
        time.sleep(2)
    states = [task["status"] for task in batch["tasks"]]
    assert states == ["waiting_login"], states
    checks.append("beat_dispatch_worker_and_waiting_login")
    request("POST", "/api/v1/auth/logout", {})
    request("GET", "/api/v1/auth/me", expected=401)
    checks.append("revocable_session")
    result = {"checks": checks, "company_id": company["id"], "batch_id": run["batch_id"],
              "task_states": states, "llm_calls": 0, "external_collection_requests": 0}
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
