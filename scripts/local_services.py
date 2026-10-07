"""Start only project-owned native services. Run inside hr-research-dev Conda."""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import redis
from app.config import ROOT, get_settings
from psycopg import sql
from sqlalchemy.engine import make_url

RUNTIME = ROOT / ".runtime"
SERVICE_BIN = Path(os.environ.get(
    "HR_SERVICE_BIN", str(Path(sys.prefix).parent / "hr-research-services" / "bin")
))
PGDATA = RUNTIME / "postgres"
REDISDATA = RUNTIME / "redis"


def command(name, *args, check=True):
    executable = SERVICE_BIN / name
    if not executable.is_file():
        raise RuntimeError(f"缺少项目原生服务程序：{executable}")
    return subprocess.run([str(executable), *map(str, args)], check=check,
                          capture_output=True, text=True)


def start():
    settings = get_settings()
    db_url = make_url(settings.database_url.get_secret_value())
    redis_url = make_url(settings.redis_url.get_secret_value())
    if db_url.host != "127.0.0.1" or db_url.port != 55432 or redis_url.host != "127.0.0.1" or redis_url.port != 56379:
        raise RuntimeError("此脚本只管理本项目约定的本机专用端口")
    RUNTIME.mkdir(exist_ok=True, mode=0o700)
    REDISDATA.mkdir(exist_ok=True, mode=0o700)
    password_file = RUNTIME / "postgres-password"
    password_file.write_text(db_url.password)
    password_file.chmod(0o600)
    if not (PGDATA / "PG_VERSION").exists():
        command("initdb", "-D", PGDATA, "-U", db_url.username, "-A", "scram-sha-256",
                "--pwfile", password_file, "--encoding=UTF8", "--locale=C")
    if command("pg_ctl", "-D", PGDATA, "status", check=False).returncode:
        command("pg_ctl", "-D", PGDATA, "-l", RUNTIME / "postgres.log",
                "-o", "-h 127.0.0.1 -p 55432 -k " + str(RUNTIME), "start")
    connection = dict(host=db_url.host, port=db_url.port, user=db_url.username,
                      password=db_url.password, dbname="postgres")
    with psycopg.connect(**connection, autocommit=True) as conn:
        for name in (db_url.database, make_url(settings.test_database_url.get_secret_value()).database):
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    client = redis.Redis.from_url(settings.redis_url.get_secret_value())
    try:
        client.ping()
    except redis.ConnectionError:
        config = RUNTIME / "redis.conf"
        config.write_text(
            "bind 127.0.0.1\nport 56379\nprotected-mode yes\n"
            f"requirepass {redis_url.password}\n"
            f"dir {REDISDATA}\nlogfile {RUNTIME / 'redis.log'}\n"
            f"pidfile {RUNTIME / 'redis.pid'}\n"
            "daemonize yes\nappendonly yes\nsave 60 1\n"
        )
        config.chmod(0o600)
        command("redis-server", config)
        for _ in range(30):
            try:
                client.ping()
                break
            except redis.ConnectionError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Redis 未就绪")
    print("Project PostgreSQL :55432 and Redis :56379 ready. No Docker used.")


def stop():
    if not (PGDATA / "PG_VERSION").exists():
        raise RuntimeError("找不到本项目服务目录，拒绝停止其他服务")
    command("pg_ctl", "-D", PGDATA, "-m", "fast", "stop", check=False)
    settings = get_settings()
    url = make_url(settings.redis_url.get_secret_value())
    if url.host == "127.0.0.1" and url.port == 56379 and (RUNTIME / "redis.pid").exists():
        try:
            redis.Redis.from_url(settings.redis_url.get_secret_value()).shutdown()
        except redis.ConnectionError:
            pass
    print("Project services stopped.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop"])
    args = parser.parse_args()
    try:
        start() if args.action == "start" else stop()
    except Exception as exc:
        # Subprocess/connection exceptions can contain credential-bearing parameters.
        print("Local services failed:", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
