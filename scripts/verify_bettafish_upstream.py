"""Run the pinned, unchanged ReportAgent with synthetic IPC responses; zero paid calls.

This checks the real upstream pipeline, unlike the lightweight protocol fixture.
It does not activate the production Bridge or assert enterprise-use authorization.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace
import uuid

from app.bettafish_bridge import BettaFishBridge, PINNED_REVISION


class SyntheticGateway:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        if len(self.calls) >= 8:
            raise RuntimeError("offline_fixture_call_limit")
        payload = json.loads(messages[-1]["content"])
        self.calls.append({"input_chars": sum(len(message["content"]) for message in messages),
                           "max_output_tokens": kwargs["max_output_tokens"],
                           "payload_keys": list(payload)})
        if "template" in payload:
            sections = payload["template"]["sections"]
            response = {"title": payload["query"], "hero": {"title": payload["query"]},
                        "tocTitle": "样本报告目录", "tocPlan": [
                            {"chapterId": s["chapterId"], "title": s["title"], "anchor": s["slug"]}
                            for s in sections]}
        elif "design" in payload and "sections" in payload:
            response = {"totalWords": 600, "globalGuidelines": ["只描述离线合成样例，不作真实舆情结论。"],
                        "chapters": [{"chapterId": s["chapterId"], "title": s["title"], "targetWords": 300,
                                      "minWords": 100, "maxWords": 600} for s in payload["sections"]]}
        elif "section" in payload:
            section = payload["section"]
            content = ("这是一条用于验证真实上游运行的合成资料。它不代表任何公司的真实客户评价，"
                       "不用于判断经营情况或估算全平台声量。报告保留采样局限，所有内容尚未经过事实核实。")
            response = {"chapterId": section["chapterId"], "title": section["title"],
                        "anchor": section["slug"], "order": section["order"],
                        "blocks": [{"type": "heading", "level": 2, "text": section["title"],
                                    "anchor": section["slug"]},
                                   {"type": "paragraph", "inlines": [{"text": content * 6}]},
                                   {"type": "paragraph", "inlines": [{"text": "合成样例原文链接",
                                     "marks": [{"type": "link", "href": "https://example.invalid/evidence/1"}]}]}]}
        else:
            raise RuntimeError("unexpected_upstream_stage")
        return SimpleNamespace(content=json.dumps(response, ensure_ascii=False), call_id=uuid.uuid4())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(".runtime/bettafish-validation"))
    parser.add_argument("--template", choices=["compact", "multi"], default="compact")
    parser.add_argument("--max-calls", type=int, default=3,
                        help="Synthetic-response task limit; never changes application/model settings")
    args = parser.parse_args()
    repo = args.repo.resolve()
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"],
                           capture_output=True, text=True, check=True).stdout.strip()
    if revision != PINNED_REVISION or dirty:
        raise SystemExit("A clean checkout of the pinned revision is required")
    task_id = uuid.uuid4()
    workdir = args.output.resolve() / str(task_id)
    workdir.mkdir(parents=True, mode=0o700)
    request = {"task_id": str(task_id), "query": "BettaFish真实上游离线合成验证",
               "evidence": [{"item_id": "synthetic-1", "title": "离线样例",
                             "url": "https://example.invalid/evidence/1", "content": "合成样例，无真实客户信息。"}],
               "coverage": {"sample_size": 1, "verification": "offline_synthetic"},
               "template": "# 企业声誉观察\n### 已采集样本与主要反馈\n### 覆盖局限与待核实事项"
               if args.template == "compact" else "# 企业声誉观察\n## 已采集样本与主要反馈\n## 覆盖局限与待核实事项"}
    request_file = workdir / "request.json"
    request_file.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    request_file.chmod(0o600)
    # Preserve a venv's executable symlink; resolving it would launch the base interpreter.
    settings = SimpleNamespace(bettafish_python=str(args.python.expanduser().absolute()), bettafish_repo_dir=str(repo),
                               bettafish_revision=revision, bettafish_timeout_seconds=90,
                               llm_max_output_tokens=1200, llm_task_call_limit=args.max_calls,
                               llm_daily_request_limit=12, llm_max_input_chars=16000)
    gateway = SyntheticGateway()
    started = time.monotonic()
    evidence = {"revision": revision, "real_upstream": True, "paid_model_calls": 0,
                "template": args.template, "task_id": str(task_id), "workdir": str(workdir)}
    try:
        result = BettaFishBridge(settings)._run_process(task_id, request_file, workdir, gateway, None)
        assert "合成" in result["html"]
        evidence.update(status="passed", html_bytes=len(result["html"].encode()),
                        artifact_sha256=result["sha256"], model_responses="deterministic_synthetic")
        evidence["preflight"] = result.get("requirements")
        exit_code = 0
    except Exception as exc:
        evidence.update(status="failed", error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=getattr(exc, "message", type(exc).__name__))
        exit_code = 1
    evidence.update(seconds=round(time.monotonic() - started, 3), calls=gateway.calls)
    (workdir / "validation.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
