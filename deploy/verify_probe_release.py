"""Read-only release checks; run only in this project's authorized remote containers.

Login/logout create only internal access sessions. No pairing, device, source,
task, model request or business fixture is created. Never print credentials.
"""

import argparse
import hashlib
import json
import ssl
import sys
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

sys.path.insert(0, "/app/backend")

TABLES = (
    "companies", "watch_subjects", "source_bindings", "source_accounts", "jobs",
    "job_versions", "job_observations", "raw_documents", "sentiment_items",
    "sentiment_observations", "model_calls", "monitoring_plans", "task_runs",
)


def snapshot(database=None):
    from app.config import get_settings
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = make_url(get_settings().database_url.get_secret_value())
    if database:
        if not database.endswith("_test"):
            raise ValueError("Baseline database must be isolated *_test")
        url = url.set(database=database)
    engine = create_engine(url)
    with engine.connect() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        return {
            table: dict(connection.execute(text(
                f"SELECT count(*) AS count, md5(coalesce(jsonb_agg(to_jsonb(t) "
                f"ORDER BY id)::text, '[]')) AS digest FROM {table} t"
            )).mappings().one())
            for table in TABLES
        }


def http_checks(base, ca):
    from app.config import get_settings
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    settings = get_settings()
    context = ssl.create_default_context(cafile=str(ca))
    jar = CookieJar()
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar), HTTPSHandler(context=context))

    def call(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=30) as response:
            assert response.status == 200, "Unexpected HTTP status"
            return json.loads(response.read())

    assert opener.open(base + "/healthz", timeout=15).status == 200
    call("/api/v1/auth/login", {"username": settings.admin_username,
                               "password": settings.admin_password.get_secret_value()})
    try:
        releases = call("/api/v1/probes/downloads")
        assert releases["server"] == base
        assert len(releases["releases"]) == 3
        downloads = []
        for release in releases["releases"]:
            digest = hashlib.sha256()
            size = 0
            with opener.open(base + "/api/v1/probes/downloads/" + release["id"], timeout=60) as response:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            assert digest.hexdigest() == release["sha256"], "Download digest differs"
            downloads.append({"id": release["id"], "bytes": size, "sha256": digest.hexdigest()})
        devices = call("/api/v1/probes/devices")
        profiles = call("/api/v1/probes/profiles")
        # A refused upgrade must reach the authenticated ASGI route through both proxies.
        try:
            with connect(base.replace("https://", "wss://") + "/api/v1/probes/agent/ws",
                         additional_headers={"Authorization": "Bearer invalid-release-check"},
                         ssl=context, open_timeout=15, proxy=None):
                raise AssertionError("Unauthenticated WebSocket accepted")
        except InvalidStatus as error:
            assert error.response.status_code == 403, "Unexpected WSS rejection"
        else:
            raise AssertionError("Unauthenticated WebSocket accepted")
        return {"tls": "verified", "downloads": downloads, "wss_auth_rejection": 403,
                "devices": len(devices), "profiles": len(profiles), "business_writes": 0,
                "model_requests": 0, "native_validation": "not_run"}
    finally:
        call("/api/v1/auth/logout", {})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-db")
    parser.add_argument("--baseline-file", type=Path)
    parser.add_argument("--http-base")
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"snapshot": snapshot(args.baseline_db)}
    if args.baseline_file:
        baseline = json.loads(args.baseline_file.read_text())["snapshot"]
        result["business_preserved"] = result["snapshot"] == baseline
        assert result["business_preserved"], "Business tables changed"
    if args.http_base:
        result["http"] = http_checks(args.http_base, args.ca)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    args.output.chmod(0o600)
    print(json.dumps({"status": "passed", "business_preserved": result.get("business_preserved"),
                      "tables": len(result["snapshot"]), "http": result.get("http")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
