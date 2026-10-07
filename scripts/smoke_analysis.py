"""One explicitly requested real analysis, using only the existing two-job development sample."""
import argparse
import json
import time
import uuid

import httpx
from sqlalchemy import select

from app.config import ROOT, get_settings
from app.db import session_factory
from app.models import ModelCall


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--allow-repeat", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    with httpx.Client(base_url="http://127.0.0.1:8000/api/v1", timeout=30, trust_env=False) as client:
        response = client.post("/auth/login", json={
            "username": settings.admin_username, "password": settings.admin_password.get_secret_value(),
        })
        response.raise_for_status()
        used = client.get("/system/status").json()["llm"]["calls_today"]
        if used and not args.allow_repeat:
            raise RuntimeError("今日已有真实模型调用；不会重复测试，需明确 --allow-repeat")
        company = next(c for c in client.get("/companies").json()
                       if c["name"] == "腾讯（开发验证样本）" and c["role"] == "other")
        response = client.post("/companies/" + company["id"] + "/analysis")
        response.raise_for_status()
        task_id = response.json()["task_id"]
        print(json.dumps({"task_id": task_id, "max_model_calls": 1, "max_output_tokens": 900}), flush=True)
        for _ in range(60):
            response = client.get("/tasks/" + task_id)
            response.raise_for_status()
            task = response.json()
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(2)
        with session_factory()() as db:
            calls = db.scalars(select(ModelCall).where(ModelCall.task_id == uuid.UUID(task_id))).all()
            usage = [{"status": c.status, "model": c.model, "prompt_tokens": c.prompt_tokens,
                      "completion_tokens": c.completion_tokens, "error": c.error_code,
                      "reasoning_tokens": c.reasoning_tokens, "finish_reason": c.finish_reason} for c in calls]
        report_id = task["summary"].get("report_id")
        report = client.get("/reports/" + report_id).json() if report_id else None
        result = {"task_id": task_id, "status": task["status"], "error": task["error_message"],
                  "usage": usage, "report": report}
        (ROOT / ".runtime" / "analysis-smoke.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if task["status"] != "succeeded":
            raise RuntimeError("真实模型验证未完成；已记录原因，不自动重试")


if __name__ == "__main__":
    main()
