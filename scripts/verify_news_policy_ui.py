"""Built notification UI, localhost fixtures and real RuleInput; no deliveries."""
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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
CHANNEL = "00000000-0000-4000-8000-000000000301"
RULE = "00000000-0000-4000-8000-000000000302"
STAMP = "2026-10-20T02:00:00Z"
EMPTY = {"regions": [], "industries": [], "topics": []}
ORIGINAL = {"regions": ["上海"], "industries": ["制造业"], "topics": ["用工政策"]}


class FixtureAPI:
    def __init__(self):
        self.writes, self.requests = [], []
        self.rules = [{"id": RULE, "name": "QA资讯规则", "channel_id": CHANNEL, "event_types": ["news.new"],
            "news_filters": deepcopy(ORIGINAL), "subject_ids": [], "keywords": [], "enabled": False,
            "delivery_mode": "immediate", "digest_time": "09:00", "timezone": "Asia/Shanghai", "cooldown_seconds": 0}]

    def request(self, method, path, body):
        from app.notification_api import RuleInput
        self.requests.append({"method": method, "path": path})
        if method == "GET":
            if path == "/api/v1/auth/me":
                return 200, {"username": "隔离QA", "role": "admin"}
            if path == "/api/v1/notification-channels":
                return 200, [{"id": CHANNEL, "name": "QA关闭通道", "kind": "webhook", "destination_hint": "fixture.invalid", "enabled": False}]
            if path == "/api/v1/notification-rules":
                return 200, self.rules
            if path in {"/api/v1/subjects", "/api/v1/notification-deliveries", "/api/v1/notification-digest-pending"}:
                return 200, []
            if path == "/api/v1/news-policy-reminders":
                return 200, [{"id": "policy-" + status, "item_id": "item-" + status, "title": "QA政策原文-" + status,
                    "url": "https://fixture.invalid/policy", "effective_date": "2026-11-01", "scheduled_for": "2026-11-01T01:00:00Z",
                    "status": status, "error_code": "policy_changed" if status == "cancelled" else None,
                    "event_id": "event-fixture" if status != "pending" else None} for status in ["pending", "emitted", "cancelled"]]
        if method in {"POST", "PUT"} and path.startswith("/api/v1/notification-rules"):
            valid = RuleInput.model_validate(body).model_dump(mode="json")
            result = {**valid, "id": RULE if method == "PUT" else "00000000-0000-4000-8000-000000000303"}
            self.writes.append({"method": method, "body": deepcopy(body), "validated": valid})
            if method == "PUT":
                self.rules[0] = result
            else:
                self.rules.append(result)
            return 200, result
        return 405, {"detail": "fixture rejects sending, test channels and all unregistered operations"}


class Handler(SimpleHTTPRequestHandler):
    api: FixtureAPI

    def log_message(self, *_):
        pass

    def dispatch(self):
        path = urlparse(self.path).path
        if path.startswith("/api/"):
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16000:
                self.send_error(413)
                return
            body = json.loads(self.rfile.read(length)) if length else {}
            status, value = self.api.request(self.command, path, body)
            data = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.command != "GET":
            self.send_error(405)
            return
        if path == "/notifications":
            self.path = "/index.html"
        super().do_GET()

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch


def choose(page, label, value):
    field = page.get_by_role("dialog").get_by_label(label, exact=True)
    field.locator("xpath=ancestor::div[contains(@class, 'ant-select-selector')]").click()
    if field.get_attribute("readonly") is None:
        field.fill(value)
    page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(has=page.get_by_text(value, exact=True)).click()
    page.get_by_label("规则名称", exact=True).click()


def tags(page, name, values):
    field = page.locator("#news_filters_" + name)
    item = page.locator(".ant-form-item").filter(has=field)
    if item.locator(".ant-select-clear").count():
        item.locator(".ant-select").hover()
        item.locator(".ant-select-clear").click()
    for value in values:
        field.fill(value)
        field.press("Enter")
    page.get_by_label("规则名称", exact=True).click()


def edit(page):
    page.locator('tr[data-row-key="' + RULE + '"]').get_by_role("button", name=re.compile(r"^编\s*辑$")).click()


def save(page, expect):
    with page.expect_response(lambda response: response.request.method in {"POST", "PUT"} and "/notification-rules" in response.url) as response:
        page.get_by_role("button", name="保存规则", exact=True).click()
    assert response.value.status == 200
    expect(page.get_by_role("dialog")).not_to_be_visible()


def screen(page, folder, name, expect):
    page.get_by_label("规则名称", exact=True).click()
    expect(page.locator(".ant-select-dropdown:visible")).to_have_count(0)
    page.screenshot(path=str(folder / (name + ".png")))


def verify(page, fixture, folder, expect):
    checks = []
    page.get_by_role("tab", name="订阅规则", exact=True).click()
    edit(page)
    page.get_by_label("规则名称", exact=True).fill("QA仅修改名称")
    save(page, expect)
    assert fixture.writes[-1]["validated"]["news_filters"] == ORIGINAL
    checks.append("edit_name_preserves_news_filters_and_disabled_state")
    edit(page)
    tags(page, "regions", ["北京", "上海"])
    tags(page, "industries", ["IT", "制造业"])
    tags(page, "topics", ["社保", "劳动关系"])
    screen(page, folder, "structured-filters", expect)
    save(page, expect)
    valid = fixture.writes[-1]["validated"]
    assert valid["news_filters"] == {"regions": ["上海", "北京"], "industries": ["it", "制造业"], "topics": ["劳动关系", "社保"]}
    assert valid["enabled"] is False
    checks.append("three_tag_dimensions_are_sent_and_normalized_by_real_rule_input")
    edit(page)
    before = len(fixture.writes)
    choose(page, "关注事件", "岗位首次发现")
    page.get_by_role("button", name="保存规则", exact=True).click()
    expect(page.get_by_text("资讯标签条件只能搭配资讯事件；其他事件请单独建规则", exact=True)).to_be_visible()
    assert len(fixture.writes) == before
    page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^取\s*消$")).click()
    checks.append("mixed_non_news_events_with_news_filters_are_rejected_before_http")
    edit(page)
    for name in EMPTY:
        tags(page, name, [])
    save(page, expect)
    assert fixture.writes[-1]["validated"]["news_filters"] == EMPTY
    checks.append("clearing_filters_persists_empty_arrays")
    page.get_by_role("button", name="新增规则", exact=True).click()
    page.get_by_label("规则名称", exact=True).fill("QA生效日提醒")
    choose(page, "通知通道", "QA关闭通道")
    choose(page, "提醒方式", "每日定时摘要")
    choose(page, "关注事件", "政策生效日提醒")
    delivery = page.locator(".ant-form-item").filter(has=page.locator("#delivery_mode"))
    expect(delivery.get_by_text("即时提醒", exact=True)).to_be_visible()
    expect(page.get_by_text("生效日提醒只能使用即时模式", exact=True)).not_to_be_visible()
    expect(page.get_by_text(re.compile("原文生效日北京时间09:00"))).to_be_visible()
    screen(page, folder, "policy-rule-default-off", expect)
    switch = page.get_by_label("启用规则", exact=True)
    switch.scroll_into_view_if_needed()
    expect(switch).to_have_attribute("aria-checked", "false")
    page.screenshot(path=str(folder / "policy-rule-activation.png"))
    save(page, expect)
    assert fixture.writes[-1]["validated"]["event_types"] == ["news.policy_effective"]
    assert fixture.writes[-1]["validated"]["delivery_mode"] == "immediate"
    assert fixture.writes[-1]["validated"]["news_filters"] == EMPTY and fixture.writes[-1]["validated"]["enabled"] is False
    checks.append("policy_event_forces_immediate_without_enabling_rule_or_channel")
    page.get_by_role("tab", name="政策日期提醒", exact=True).click()
    expect(page.get_by_text("已生成通知事件", exact=True)).to_be_visible()
    expect(page.get_by_text("实际发送结果见投递记录", exact=True)).to_be_visible()
    expect(page.get_by_text("日期或依据已变化，旧提醒不再发送", exact=True)).to_be_visible()
    expect(page.locator('tr[data-row-key="policy-pending"]').get_by_text("2026-11-01 09:00", exact=True)).to_be_visible()
    page.wait_for_timeout(300)  # Let the tab indicator finish its visual transition.
    page.screenshot(path=str(folder / "policy-statuses.png"))
    checks.append("policy_status_does_not_claim_sent_and_due_time_is_beijing_in_new_york_browser")
    assert not any("/test" in request["path"] or "/retry" in request["path"] for request in fixture.requests)
    return checks


def main():
    parser = ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", default=str(ROOT / ".runtime/news-policy-validation-20261005/ui"))
    args = parser.parse_args()
    if not args.execute:
        print("Plan: actual built UI plus local fixtures/RuleInput, no database or notification sends.")
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
    result = {"scope": "built UI + localhost HTTP fixtures + production RuleInput", "database_writes": 0,
              "model_calls": 0, "real_notifications": 0, "external_calls": 0,
              "browser_timezone": "America/New_York",
              "source_sha256": hashlib.sha256((ROOT / "frontend/src/NotificationsPage.tsx").read_bytes()).hexdigest()}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN", timezone_id="America/New_York", service_workers="block")
            def guard(route):
                parsed = urlparse(route.request.url)
                if parsed.scheme + "://" + parsed.netloc != origin:
                    blocked.append(parsed.path)
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", guard)
            context.add_init_script("document.addEventListener('DOMContentLoaded',()=>{let e=document.createElement('div');e.textContent='隔离QA夹具 · 未发送通知';e.style='position:fixed;top:0;right:0;z-index:99999;background:#ffe58f;padding:6px';document.body.append(e);})")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({"path": urlparse(response.url).path, "status": response.status}) if "/api/" in response.url else None)
            page.goto(origin + "/notifications", wait_until="networkidle")
            result["checks"] = verify(page, fixture, folder, expect)
            assert not errors and not blocked and all(response["status"] < 400 for response in responses)
            result["passed"] = True
            browser.close()
    except Exception as exc:
        result.update(passed=False, failure=str(exc), failure_type=type(exc).__name__)
        raise
    finally:
        server.shutdown()
        server.server_close()
        result.update(writes=fixture.writes, requests=fixture.requests, api_responses=responses, js_errors=errors, denied_requests=blocked)
        (folder / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": True, "checks": len(result["checks"]), "summary": str(folder / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
