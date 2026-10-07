"""Verify upgrading existing rows in a fresh, dedicated local PostgreSQL QA database.

Never runs the application, retention, a model, or a notification transport.
The QA database and evidence are retained. No command targets the main database.
"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
OLD_HEAD = "7c8ba429a6ea"
NEW_HEAD = "b25d0c8f3142"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def main():
    import psycopg
    from psycopg import sql
    from alembic import command
    from alembic.config import Config
    from pydantic import SecretStr
    from sqlalchemy import MetaData, Table, create_engine, select, text
    from sqlalchemy.engine import make_url
    from app.config import get_settings

    assert Path(sys.prefix).name == "hr-research-dev", "Project Conda is required"
    settings = get_settings()
    source = make_url(settings.database_url.get_secret_value())
    assert source.host in {"127.0.0.1", "localhost"} and source.drivername.startswith("postgresql")
    run_id = uuid.uuid4().hex[:12]
    database = f"hr_lifecycle_migration_{run_id}_test"
    folder = ROOT / ".runtime" / f"lifecycle-migration-{run_id}"
    folder.mkdir(mode=0o700)
    plan = folder / "plan.json"
    plan.write_text(json.dumps({"database": database, "from_revision": OLD_HEAD, "to_revision": NEW_HEAD}))
    plan.chmod(0o600)
    settings.app_env = "test"
    settings.llm_api_key = SecretStr("migration-qa-not-a-real-key")
    settings.llm_base_url = "https://models.example.invalid/v1"
    settings.database_url = SecretStr(source.set(database=database).render_as_string(hide_password=False))
    with psycopg.connect(host=source.host, port=source.port or 5432, user=source.username,
                         password=source.password, dbname="postgres", autocommit=True) as conn:
        assert not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone()
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    config = Config(str(ROOT / "backend/alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend/migrations"))
    logs = io.StringIO()
    with redirect_stdout(logs), redirect_stderr(logs):
        command.upgrade(config, OLD_HEAD)
    engine = create_engine(settings.database_url.get_secret_value())
    assert engine.url.database == database
    stamp = datetime.now(timezone.utc)
    base = {"created_at": stamp, "updated_at": stamp}
    ids = {name: uuid.uuid4() for name in ("task", "channel", "rule", "raw", "report")}
    rows = {
        "task_runs": {**base, "id": ids["task"], "domain": "news", "kind": "news_collection",
                      "status": "succeeded", "config_snapshot": {}, "scope_hash": "migration-fixture",
                      "progress": 100, "attempt": 1, "summary": {"kept": True}, "cancel_requested": False},
        "notification_channels": {**base, "id": ids["channel"], "name": "Migration QA disabled",
                                  "kind": "webhook", "encrypted_config": "inert-not-a-secret",
                                  "destination_hint": "example.invalid", "enabled": False},
        "notification_rules": {**base, "id": ids["rule"], "name": "Legacy rule", "channel_id": ids["channel"],
                               "event_types": ["news.new"], "subject_ids": [], "keywords": ["薪酬"],
                               "cooldown_seconds": 0, "delivery_mode": "daily_digest", "digest_time": "09:00",
                               "timezone": "Asia/Shanghai", "enabled": False},
        "raw_documents": {**base, "id": ids["raw"], "task_id": ids["task"], "url": "https://example.invalid/raw",
                          "sha256": "a" * 64, "storage_ref": "tasks/inert-fixture.txt", "content_type": "text/plain"},
        "analysis_reports": {**base, "id": ids["report"], "task_id": ids["task"], "domain": "news",
                             "title": "Legacy QA report", "content": "Preserve this existing report body",
                             "evidence": [{"url": "https://example.invalid/raw"}], "coverage": {"fixture": True}},
    }
    old = MetaData()
    tables = {name: Table(name, old, autoload_with=engine) for name in rows}
    with engine.begin() as conn:
        for name, value in rows.items():
            conn.execute(tables[name].insert().values(**value))
        before = {name: [dict(r) for r in conn.execute(select(table)).mappings()] for name, table in tables.items()}
    before_sha = fingerprint(before)
    migration_start = datetime.now(timezone.utc)
    with redirect_stdout(logs), redirect_stderr(logs):
        command.upgrade(config, NEW_HEAD)
        command.check(config)
    migration_end = datetime.now(timezone.utc)
    with engine.connect() as conn:
        after = {name: [dict(r) for r in conn.execute(select(table)).mappings()] for name, table in tables.items()}
        assert fingerprint(after) == before_sha
        assert conn.scalar(text("SELECT news_filters FROM notification_rules")) == {"regions": [], "industries": [], "topics": []}
        assert conn.execute(text("SELECT cleared_at,retention FROM raw_documents")).one() == (None, {})
        assert conn.execute(text("SELECT cleared_at,retention FROM analysis_reports")).one() == (None, {})
        assert conn.execute(text("SELECT logs_cleared_at,log_retention FROM task_runs")).one() == (None, {})
        policies = [dict(r) for r in conn.execute(text("SELECT * FROM retention_policies ORDER BY category")).mappings()]
        assert {p["category"]: p["retention_days"] for p in policies} == {
            "raw_documents": 90, "reports": 180, "task_logs": 90, "expired_sessions": 7}
        assert all(p["enabled"] and p["revision"] == 1 and
                   migration_start + timedelta(days=7) <= p["grace_until"] <= migration_end + timedelta(days=7)
                   for p in policies)
        policy_sha = fingerprint(policies)
    with redirect_stdout(logs), redirect_stderr(logs):
        command.upgrade(config, "head")
    with engine.connect() as conn:
        assert fingerprint([dict(r) for r in conn.execute(text("SELECT * FROM retention_policies ORDER BY category")).mappings()]) == policy_sha
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == NEW_HEAD
    engine.dispose()
    result = {"status": "passed", "database": database, "from_revision": OLD_HEAD, "to_revision": NEW_HEAD,
              "legacy_tables_preserved": list(rows), "legacy_rows_sha256": before_sha,
              "legacy_rule_filters_unrestricted": True, "retention_defaults_and_seven_day_grace": True,
              "second_upgrade_preserves_policy_ids_and_grace": True, "alembic_check": "no_drift",
              "main_database_writes": 0, "cleanup_executions": 0, "model_calls": 0, "notifications": 0,
              "migration_sources": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                    for path in (ROOT / "backend/migrations/versions").glob("*.py")
                                    if path.name.startswith(("a14c9b7e2031", "b25d0c8f3142"))}}
    output = folder / "summary.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    output.chmod(0o600)
    print(json.dumps({"status": "passed", "evidence": str(output), "main_database_writes": 0}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Connection errors may embed private configuration; never forward their text.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
