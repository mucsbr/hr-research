"""Explicit bounded live smoke test: one official list page, at most two job details."""
import argparse
import json
import time

import httpx

from app.config import ROOT, get_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    args = parser.parse_args()
    assert args.live
    settings = get_settings()
    with httpx.Client(base_url="http://127.0.0.1:8000/api/v1", timeout=30, trust_env=False) as client:
        response = client.post("/auth/login", json={
            "username": settings.admin_username,
            "password": settings.admin_password.get_secret_value(),
        })
        response.raise_for_status()
        companies = client.get("/companies").json()
        company = next((c for c in companies if c["name"] == "腾讯（开发验证样本）"), None)
        if not company:
            response = client.post("/companies", json={
                "name": "腾讯（开发验证样本）", "role": "other",
                "website": "https://careers.tencent.com/",
                "notes": "仅验证公开招聘采集链路，不是用户确认的竞品；通知未启用。",
            })
            response.raise_for_status()
            company = response.json()
        bindings = client.get("/source-bindings", params={"company_id": company["id"]}).json()
        if not bindings:
            response = client.post("/source-bindings", json={
                "company_id": company["id"], "kind": "careers", "adapter": "tencent",
                "entry_url": "https://careers.tencent.com/search.html?query=at_1",
            })
            response.raise_for_status()
        response = client.post("/recruitment/runs", json={
            "company_ids": [company["id"]], "name": "官网最小真实链路验证（2 条上限）",
            "filters": {"max_pages": 1, "max_details": 2, "sources": ["careers"]},
        })
        response.raise_for_status()
        batch_id = response.json()["batch_id"]
        print(json.dumps({"batch_id": batch_id, "max_details": 2}, ensure_ascii=False), flush=True)
        for _ in range(60):
            response = client.get("/batches/" + batch_id)
            response.raise_for_status()
            batch = response.json()
            if batch["status"] not in {"queued", "running"}:
                break
            time.sleep(1)
        else:
            raise RuntimeError("任务等待超过 60 秒；请查看任务状态，不要重复创建任务")
        tasks = [client.get("/tasks/" + t["id"]).json() for t in batch["tasks"]]
        jobs = client.get("/jobs", params={"company_id": company["id"]}).json()
        result = {
            "batch_id": batch_id, "status": batch["status"], "task_statuses": [t["status"] for t in tasks],
            "sources": [s for t in tasks for s in t["sources"]], "job_count": jobs["total"],
            "titles": [j["title"] for j in jobs["items"]],
            "llm_calls_today": client.get("/system/status").json()["llm"]["calls_today"],
        }
        target = ROOT / ".runtime" / "recruitment-smoke.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not jobs["total"] or any(t["status"] not in {"succeeded", "partial"} for t in tasks):
            raise RuntimeError("真实链路尚未通过，见上面的来源错误与已保存记录")


if __name__ == "__main__":
    main()
