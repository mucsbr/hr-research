"""Actual built sentiment form + localhost fixtures + production RunInput validation.

Does not execute any task, connect to a database, or contact a model/site/account.
"""
from argparse import ArgumentParser
from copy import deepcopy
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
from threading import Thread
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
SUBJECTS = ["00000000-0000-4000-8000-000000000201", "00000000-0000-4000-8000-000000000202", "00000000-0000-4000-8000-000000000203"]
SOURCES = ["00000000-0000-4000-8000-000000000211", "00000000-0000-4000-8000-000000000212", "00000000-0000-4000-8000-000000000213", "00000000-0000-4000-8000-000000000214"]


class FixtureAPI:
    def __init__(self):
        self.calls, self.writes = [], []
        self.subjects = [{"id": identifier, "name": name, "type": "own_company", "active": True,
                          "aliases": [], "keywords": [], "exclusions": []}
                         for identifier, name in zip(SUBJECTS, ["QA甲公司", "QA乙公司", "QA仅导入对象"], strict=True)]
        self.sources = [{"id": identifier, "subject_id": subject, "name": name, "kind": kind,
                         "entry_urls": [], "account_id": None, "enabled": enabled, "health": "not_collected"}
                        for identifier, subject, name, kind, enabled in [
                            (SOURCES[0], SUBJECTS[0], "甲公开", "public_web", True),
                            (SOURCES[1], SUBJECTS[0], "甲微博", "wb", True),
                            (SOURCES[2], SUBJECTS[0], "甲已停用", "xhs", False),
                            (SOURCES[3], SUBJECTS[1], "乙公开", "public_web", True)]]

    def request(self, method, path, query, body):
        from app.sentiment_api import RunInput
        self.calls.append({"method": method, "path": path, "query": query})
        if method == "GET":
            if path == "/api/v1/auth/me":
                return 200, {"username": "隔离QA", "role": "admin"}
            if path == "/api/v1/sentiment/subjects":
                return 200, self.subjects
            if path == "/api/v1/sentiment/sources":
                selected = query.get("subject_id", [None])[0]
                return 200, [source for source in self.sources if not selected or source["subject_id"] == selected]
            if path == "/api/v1/sentiment/status":
                return 200, {"bettafish": {"status": "blocked_configuration", "message": "仅UI夹具，未连接报告引擎"}, "limitations": []}
            if path == "/api/v1/sentiment/items":
                return 200, {"items": [], "total": 0}
            if path in {"/api/v1/source-accounts", "/api/v1/sentiment/runs", "/api/v1/sentiment/reports"}:
                return 200, []
        if method == "POST" and path == "/api/v1/sentiment/runs":
            validated = RunInput.model_validate(body).model_dump(mode="json")
            valid_sources = {source["id"] for source in self.sources if source["enabled"] and source["subject_id"] == validated["subject_id"]}
            assert set(validated["source_ids"]) <= valid_sources, "foreign/disabled source escaped UI scope"
            assert validated["mode"] != "collect" or valid_sources, "collection without an enabled source"
            self.writes.append({"body": deepcopy(body), "validated": validated})
            return 202, {"task_id": "00000000-0000-4000-8000-000000000299", "status": "queued"}
        return 405, {"detail": "隔离夹具拒绝未登记请求，不代理真实后端"}


class Handler(SimpleHTTPRequestHandler):
    api: FixtureAPI

    def log_message(self, *_):
        pass

    def dispatch(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length)) if length else {}
            status, value = self.api.request(self.command, parsed.path, parse_qs(parsed.query), body)
            payload = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.command != "GET":
            self.send_error(405)
            return
        if parsed.path == "/sentiment":
            self.path = "/index.html"
        super().do_GET()

    do_GET = dispatch
    do_POST = dispatch


def choose(page, label, value):
    page.get_by_role("dialog").get_by_label(label, exact=True).locator(
        "xpath=ancestor::div[contains(@class, 'ant-select-selector')]").click()
    page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
        has=page.get_by_text(value, exact=True)).click()
    page.locator(".ant-modal-title").click()


def open_run(page, expect, subject="QA甲公司"):
    page.get_by_role("button", name="发起任务", exact=True).click()
    expect(page.get_by_role("dialog")).to_be_visible()
    choose(page, "关注对象", subject)
    page.wait_for_timeout(100)


def save(page, expect):
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/api/v1/sentiment/runs")) as response:
        page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^开\s*始$")).click()
    assert response.value.status == 202
    expect(page.get_by_role("dialog")).not_to_be_visible()


def screenshot(page, folder, name, expect):
    # A physical focus change closes Select; wait through its exit animation.
    # Escape during an already closing popup would instead close the Modal.
    page.get_by_label("开始时间（北京时间）", exact=True).click()
    expect(page.locator(".ant-select-dropdown:visible")).to_have_count(0)
    page.get_by_role("dialog").wait_for(state="visible")
    page.screenshot(path=str(folder / (name + ".png")))


def verify(page, api, folder, expect):
    cases = []
    open_run(page, expect)
    choose(page, "本次来源", "甲公开 · 公开网页 / 论坛")
    page.get_by_label("开始时间（北京时间）", exact=True).fill("2026-10-01T09:30")
    page.get_by_label("结束时间（北京时间）", exact=True).fill("2026-10-02T10:45")
    page.get_by_label("每来源总页面预算", exact=True).fill("5")
    expect(page.get_by_text(re.compile("未知发布时间仍保留"))).to_be_visible()
    screenshot(page, folder, "collection-scope", expect)
    save(page, expect)
    body = api.writes[-1]["validated"]
    assert body["source_ids"] == [SOURCES[0]] and body["max_pages"] == 5
    assert body["time_from"] == "2026-10-01T01:30:00Z" and body["time_to"] == "2026-10-02T02:45:00Z"
    cases.append("collection_scope_and_beijing_to_utc_in_non_beijing_browser")

    open_run(page, expect)
    choose(page, "本次来源", "甲微博 · 微博")
    choose(page, "关注对象", "QA乙公司")
    field = page.locator(".ant-form-item").filter(has=page.locator("#source_ids"))
    expect(field.locator(".ant-select-selection-item")).to_have_count(0)
    page.get_by_label("本次来源", exact=True).click()
    expect(page.locator(".ant-select-dropdown:visible").get_by_text("乙公开 · 公开网页 / 论坛", exact=True)).to_be_visible()
    expect(page.locator(".ant-select-dropdown:visible").get_by_text("甲微博 · 微博", exact=True)).to_have_count(0)
    page.locator(".ant-modal-title").click()
    save(page, expect)
    assert api.writes[-1]["validated"]["subject_id"] == SUBJECTS[1] and api.writes[-1]["validated"]["source_ids"] == []
    cases.append("subject_change_clears_other_company_sources_and_blank_collect_means_all_enabled")

    open_run(page, expect)
    page.get_by_label("本次来源", exact=True).click()
    expect(page.locator(".ant-select-dropdown:visible").get_by_text(re.compile("甲已停用"))).to_have_count(0)
    page.locator(".ant-modal-title").click()
    choose(page, "任务类型", "分析已有证据（使用模型预算）")
    choose(page, "本次来源", "甲微博 · 微博")
    page.get_by_label("开始时间（北京时间）", exact=True).fill("2026-10-01T00:00")
    page.get_by_label("分析主题", exact=True).fill("客户评价与售后线索")
    expect(page.get_by_text(re.compile("未知时按首次发现时间"))).to_be_visible()
    screenshot(page, folder, "analysis-scope", expect)
    save(page, expect)
    assert api.writes[-1]["validated"]["mode"] == "analyze" and api.writes[-1]["validated"]["time_to"] is None
    assert api.writes[-1]["validated"]["time_from"] == "2026-09-30T16:00:00Z"
    cases.append("analysis_selected_source_one_sided_period_and_unknown_date_explanation")

    open_run(page, expect, "QA仅导入对象")
    page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^开\s*始$")).click()
    expect(page.get_by_text("该对象尚无启用来源，请先添加来源", exact=True)).to_be_visible()
    choose(page, "任务类型", "分析已有证据（使用模型预算）")
    save(page, expect)
    assert api.writes[-1]["validated"]["source_ids"] == [] and api.writes[-1]["validated"]["mode"] == "analyze"
    cases.append("import_only_subject_can_analyze_but_cannot_collect_without_sources")

    open_run(page, expect)
    choose(page, "任务类型", "BettaFish 报告（需完成配置）")
    choose(page, "本次来源", "甲公开 · 公开网页 / 论坛")
    page.get_by_label("结束时间（北京时间）", exact=True).fill("2026-10-03T00:00")
    save(page, expect)
    assert api.writes[-1]["validated"]["mode"] == "bettafish" and api.writes[-1]["validated"]["time_from"] is None
    assert api.writes[-1]["validated"]["time_to"] == "2026-10-02T16:00:00Z"
    cases.append("bettafish_uses_same_existing_evidence_scope_contract_without_execution")

    open_run(page, expect)
    before = len(api.writes)
    page.get_by_label("开始时间（北京时间）", exact=True).fill("2026-10-04T10:00")
    page.get_by_label("结束时间（北京时间）", exact=True).fill("2026-10-03T10:00")
    page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^开\s*始$")).click()
    expect(page.get_by_text("结束时间不能早于开始时间", exact=True)).to_be_visible()
    assert len(api.writes) == before
    page.get_by_label("开始时间（北京时间）", exact=True).fill("")
    page.get_by_label("结束时间（北京时间）", exact=True).fill("")
    for value in ["0", "6", "1.5"]:
        page.get_by_label("每来源总页面预算", exact=True).fill(value)
        page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^开\s*始$")).click()
        expect(page.get_by_text("页面预算须为1至5的整数", exact=True)).to_be_visible()
        assert len(api.writes) == before
    cases.append("reversed_period_and_noninteger_or_out_of_range_page_budgets_do_not_submit")
    page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^取\s*消$")).click()
    open_run(page, expect)
    expect(page.get_by_label("开始时间（北京时间）", exact=True)).to_have_value("")
    expect(page.get_by_label("结束时间（北京时间）", exact=True)).to_have_value("")
    expect(page.get_by_label("每来源总页面预算", exact=True)).to_have_value("2")
    expect(page.locator(".ant-form-item").filter(has=page.locator("#source_ids")).locator(".ant-select-selection-item")).to_have_count(0)
    save(page, expect)
    assert api.writes[-1]["validated"]["mode"] == "collect" and api.writes[-1]["validated"]["render"] is None
    assert api.writes[-1]["validated"]["time_from"] is None and api.writes[-1]["validated"]["time_to"] is None
    cases.append("cancel_and_reopen_resets_scope_mode_budget_and_default_render")
    return cases


def main():
    parser = ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default=str(ROOT / ".runtime/sentiment-scope-ui-validation-20261005"))
    args = parser.parse_args()
    if not args.execute:
        print("Plan: built sentiment UI, localhost-only HTTP fixture, real RunInput, zero task execution.")
        return
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/browsers")
    from playwright.sync_api import expect, sync_playwright
    folder = Path(args.output)
    folder.mkdir(parents=True, exist_ok=True)
    fixture = FixtureAPI()
    Handler.api = fixture
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    Thread(target=server.serve_forever, daemon=True).start()
    origin = "http://127.0.0.1:" + str(server.server_port)
    errors, blocked, responses = [], [], []
    result = {"scope": "built React + isolated localhost fixture + production RunInput",
              "browser_timezone": "America/New_York", "input_timezone": "UTC+08:00",
              "real_database_writes": 0, "task_executions": 0, "model_calls": 0, "external_calls": 0,
              "source_sha256": hashlib.sha256((ROOT / "frontend/src/SentimentPage.tsx").read_bytes()).hexdigest(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN",
                                          timezone_id="America/New_York", service_workers="block")
            def guard(route):
                parsed = urlparse(route.request.url)
                if parsed.scheme + "://" + parsed.netloc != origin:
                    blocked.append({"method": route.request.method, "path": parsed.path})
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", guard)
            context.add_init_script("document.addEventListener('DOMContentLoaded',()=>{let e=document.createElement('div');e.textContent='隔离 QA API 夹具 · 未执行采集/模型任务';e.style='position:fixed;top:0;right:0;z-index:99999;background:#ffe58f;padding:6px';document.body.append(e);})")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({"path": urlparse(response.url).path, "status": response.status}) if "/api/" in response.url else None)
            page.goto(origin + "/sentiment", wait_until="networkidle")
            result["cases"] = verify(page, fixture, folder, expect)
            assert not errors and not blocked and all(response["status"] < 400 for response in responses)
            result["passed"] = True
            browser.close()
    except Exception as exc:
        result.update(passed=False, failure_type=type(exc).__name__, failure=str(exc))
        raise
    finally:
        server.shutdown()
        server.server_close()
        result.update(js_errors=errors, denied_requests=blocked, api_responses=responses, writes=fixture.writes)
        (folder / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": True, "cases": len(result["cases"]), "summary": str(folder / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
