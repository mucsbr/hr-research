"""Verify actual built recruitment-plan UI requests against the production input schema.

Only an isolated localhost static server and in-memory API fixture are used.
No database, account, collector, model, notification or external endpoint is contacted.
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
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
COMPANY = "00000000-0000-4000-8000-000000000081"
PLAN = "00000000-0000-4000-8000-000000000082"
STAMP = "2026-10-05T00:00:00Z"
ORIGINAL = {"cities": ["北京", "杭州"], "keywords": ["研发", "架构"], "exclusions": ["实习"],
            "sources": ["careers", "boss"], "max_pages": 7, "max_details": 23}
DEFAULT = {"cities": [], "keywords": [], "exclusions": [], "sources": [], "max_pages": 10, "max_details": 50}


class FixtureAPI:
    def __init__(self):
        self.calls, self.writes = [], []
        self.search_writes = []
        self.search_config = {"endpoint_url": "http://recruitment-search:8080/search", "enabled": True, "max_results": 5}
        self.plans = [{"id": PLAN, "name": "QA已有筛选计划", "company_ids": [COMPANY],
                       "schedule": "15 8 * * *", "timezone": "Asia/Shanghai", "enabled": True,
                       "next_run_at": STAMP, "filters": deepcopy(ORIGINAL)}]

    def request(self, method, path, body):
        from app.schemas import PlanInput
        self.calls.append({"method": method, "path": path, "body": body if method != "GET" else None})
        if method == "GET":
            if path == "/api/v1/auth/me":
                return 200, {"username": "隔离QA", "role": "admin"}
            if path == "/api/v1/companies":
                return 200, [{"id": COMPANY, "name": "QA关注公司", "official_name": None,
                              "role": "competitor", "website": None, "notes": "隔离内存夹具",
                              "created_at": STAMP, "archived": False}]
            if path == "/api/v1/plans":
                return 200, self.plans
            if path == "/api/v1/system/status":
                return 200, {"database": "ready", "timezone": "Asia/Shanghai", "bettafish": {"configured": False},
                             "llm": {"configured": False, "model": "", "daily_request_limit": 0, "task_call_limit": 0,
                                     "max_output_tokens": 0, "calls_today": 0}}
            if path == "/api/v1/sentiment/status":
                return 200, {"bettafish": {"status": "blocked_configuration", "reason": "隔离夹具，未连接模型"}}
            if path == "/api/v1/settings/recruitment-discovery":
                return 200, {"configured": True, "config": self.search_config}
            if path in {"/api/v1/users", "/api/v1/audit-events"}:
                return 200, []
        if method == "PUT" and path == "/api/v1/settings/recruitment-discovery":
            assert body == {**self.search_config, "max_results": 10}
            self.search_config = deepcopy(body)
            self.search_writes.append(deepcopy(body))
            return 200, body
        if (method, path) in {("POST", "/api/v1/plans"), ("PUT", "/api/v1/plans/" + PLAN)}:
            # Validate the browser's actual HTTP body with the same schema as the real endpoint.
            validated = PlanInput.model_validate(body).model_dump(mode="json")
            identifier = PLAN if method == "PUT" else "00000000-0000-4000-8000-000000000083"
            saved = {**validated, "id": identifier, "next_run_at": STAMP if validated["enabled"] else None}
            if method == "PUT":
                self.plans[0] = saved
            else:
                self.plans.append(saved)
            self.writes.append({"method": method, "path": path, "body": deepcopy(body), "validated": validated})
            return 200, saved
        return 405, {"detail": "隔离夹具拒绝未登记请求，不代理真实后端"}


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
        if path in {"/plans", "/settings"}:
            self.path = "/index.html"
        super().do_GET()

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch


def item(page, key):
    return page.locator(".ant-form-item").filter(has=page.locator("#filters_" + key))


def clear_tags(page, key):
    field = item(page, key)
    if field.locator(".ant-select-clear").count():
        field.locator(".ant-select").hover()
        field.locator(".ant-select-clear").click()


def tags(page, key, values):
    clear_tags(page, key)
    field = page.locator("#filters_" + key)
    for value in values:
        field.fill(value)
        field.press("Enter")
    page.locator(".ant-modal-title").click()


def selected_sources(page, values):
    clear_tags(page, "sources")
    if values:
        page.locator("#filters_sources").click()
        for value in values:
            page.locator(".ant-select-dropdown:visible .ant-select-item-option").filter(
                has=page.get_by_text(value, exact=True)).click()
        page.locator(".ant-modal-title").click()


def edit(page):
    page.locator('tr[data-row-key="' + PLAN + '"]').get_by_role("button", name=re.compile(r"^编\s*辑$")).click()
    page.get_by_role("dialog").wait_for(state="visible")


def save(page, method, expect):
    with page.expect_response(lambda response: "/api/v1/plans" in response.url and response.request.method == method) as response:
        page.get_by_role("button", name="保存计划", exact=True).click()
    assert response.value.status == 200
    expect(page.get_by_role("dialog")).not_to_be_visible()


def screenshot(page, folder, name):
    page.locator(".ant-select-dropdown:visible").wait_for(state="hidden")
    page.screenshot(path=str(folder / (name + ".png")))


def verify(page, fixture, folder, expect):
    cases = []
    expect(page.get_by_role("heading", name="监测计划", exact=True)).to_be_visible()
    edit(page)
    for value in ["北京", "杭州", "研发", "架构", "实习", "招聘官网", "BOSS 直聘"]:
        expect(page.get_by_role("dialog").get_by_text(value, exact=True)).to_be_visible()
    expect(page.locator("#filters_max_pages")).to_have_value("7")
    expect(page.locator("#filters_max_details")).to_have_value("23")
    page.get_by_label("计划名称", exact=True).fill("QA仅改名称")
    save(page, "PUT", expect)
    assert fixture.writes[-1]["body"]["filters"] == ORIGINAL
    cases.append("editing_name_preserves_all_existing_filters_and_budgets")

    edit(page)
    tags(page, "cities", ["上海", "深圳"])
    tags(page, "keywords", ["产品经理", "数据分析"])
    tags(page, "exclusions", ["外包", "实习"])
    selected_sources(page, ["招聘官网", "BOSS 直聘", "猎聘", "智联招聘", "前程无忧"])
    page.locator("#filters_max_pages").fill("15")
    page.locator("#filters_max_details").fill("60")
    page.locator("#filters_cities").scroll_into_view_if_needed()
    screenshot(page, folder, "edit-filters")
    width = page.locator(".ant-modal-body").evaluate("el => ({client: el.clientWidth, scroll: el.scrollWidth})")
    assert width["scroll"] <= width["client"] + 1, width
    page.get_by_label("时区", exact=True).scroll_into_view_if_needed()
    screenshot(page, folder, "edit-budgets-and-schedule")
    save(page, "PUT", expect)
    updated = {"cities": ["上海", "深圳"], "keywords": ["产品经理", "数据分析"], "exclusions": ["外包", "实习"],
               "sources": ["careers", "boss", "liepin", "zhaopin", "51job"], "max_pages": 15, "max_details": 60}
    assert fixture.writes[-1]["body"]["filters"] == updated
    cases.append("editing_changes_every_filter_and_all_five_registered_sources")

    edit(page)
    for key in ["cities", "keywords", "exclusions", "sources"]:
        clear_tags(page, key)
    save(page, "PUT", expect)
    assert fixture.writes[-1]["body"]["filters"] == {**DEFAULT, "max_pages": 15, "max_details": 60}
    cases.append("clearing_filter_arrays_sends_empty_arrays_without_restoring_old_values")

    edit(page)
    tags(page, "cities", ["取消的草稿城市"])
    page.locator("#filters_max_pages").fill("99")
    page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^取\s*消$")).click()
    page.get_by_role("button", name="新建计划", exact=True).click()
    expect(item(page, "cities").locator(".ant-select-selection-item")).to_have_count(0)
    expect(page.locator("#filters_max_pages")).to_have_value("10")
    expect(page.locator("#filters_max_details")).to_have_value("50")
    page.get_by_label("计划名称", exact=True).fill("QA新建范围计划")
    page.get_by_role("dialog").get_by_label("关注公司", exact=True).click()
    page.locator(".ant-select-dropdown:visible").get_by_text("QA关注公司", exact=True).click()
    page.locator(".ant-modal-title").click()
    cases.append("new_plan_resets_cancelled_edit_values_to_schema_defaults")

    writes_before = len(fixture.writes)
    for key, value, error in [("max_pages", "0", "请填写 1 至 100 的整数"),
                              ("max_pages", "101", "请填写 1 至 100 的整数"),
                              ("max_details", "0", "请填写 1 至 500 的整数"),
                              ("max_details", "501", "请填写 1 至 500 的整数")]:
        page.locator("#filters_" + key).fill(value)
        page.get_by_role("button", name="保存计划", exact=True).click()
        expect(page.get_by_text(error, exact=True)).to_be_visible()
        assert len(fixture.writes) == writes_before
        page.locator("#filters_" + key).fill(str(DEFAULT[key]))
        page.locator("#filters_" + key).press("Tab")
    cases.append("out_of_range_budgets_do_not_send_save_requests")

    page.locator("#filters_cities").fill(",".join("测试城市" + str(index) for index in range(51)) + ",")
    page.locator(".ant-modal-title").click()
    page.get_by_role("button", name="保存计划", exact=True).click()
    expect(page.get_by_text("最多填写 50 个城市", exact=True)).to_be_visible()
    assert len(fixture.writes) == writes_before
    tags(page, "cities", ["上海"])
    tags(page, "keywords", ["算法"])
    tags(page, "exclusions", ["实习"])
    selected_sources(page, ["招聘官网", "智联招聘"])
    page.locator("#filters_max_pages").fill("100")
    page.locator("#filters_max_details").fill("500")
    expect(page.get_by_label("启用定时执行", exact=True)).not_to_be_checked()
    screenshot(page, folder, "create-budget-boundaries")
    save(page, "POST", expect)
    assert fixture.writes[-1]["body"]["filters"] == {
        "cities": ["上海"], "keywords": ["算法"], "exclusions": ["实习"],
        "sources": ["careers", "zhaopin"], "max_pages": 100, "max_details": 500}
    assert fixture.writes[-1]["body"]["enabled"] is False
    assert fixture.writes[-1]["body"]["timezone"] == "Asia/Shanghai"
    cases.append("tag_count_limit_and_valid_create_boundaries_use_real_http_and_production_schema")

    page.get_by_role("link", name="运行设置", exact=True).click()
    expect(page.get_by_role("heading", name="运行设置", exact=True)).to_be_visible()
    page.get_by_role("button", name="配置搜索", exact=True).click()
    endpoint = page.get_by_label("SearXNG /search 地址", exact=True)
    expect(endpoint).to_have_value("http://recruitment-search:8080/search")
    for invalid in ["ftp://recruitment-search/search", "https://name:secret@example.com/search", "not a url"]:
        endpoint.fill(invalid)
        page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^保\s*存$")).click()
        expect(page.get_by_text("请输入不含账号密码的完整 HTTP/HTTPS 地址", exact=True)).to_be_visible()
        assert not fixture.search_writes
    endpoint.fill("http://recruitment-search:8080/search")
    page.locator(".ant-form-item").filter(has=page.locator("#max_results")).locator(".ant-select-selector").click()
    page.locator(".ant-select-dropdown:visible").get_by_text("10 条", exact=True).click()
    page.locator(".ant-modal-title").click()
    screenshot(page, folder, "internal-search-endpoint")
    with page.expect_response(lambda response: response.url.endswith("/settings/recruitment-discovery") and response.request.method == "PUT") as response:
        page.get_by_role("dialog").get_by_role("button", name=re.compile(r"^保\s*存$")).click()
    assert response.value.status == 200
    expect(page.get_by_role("dialog")).not_to_be_visible()
    assert fixture.search_writes == [{"endpoint_url": "http://recruitment-search:8080/search", "enabled": True, "max_results": 10}]
    cases.append("search_url_accepts_deployed_single_label_host_preserves_endpoint_and_rejects_invalid_syntax")
    return {"cases": cases, "modal_width": width}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/plan-filters-ui-validation-20261005")
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    if not (ROOT / "frontend/dist/index.html").is_file():
        raise SystemExit("Build the current frontend before running UI verification")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/browsers")
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = FixtureAPI()
    Handler.api = fixture
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_port)
    errors, denied, responses = [], [], []
    result = {"scope": "actual built React UI + localhost HTTP fixture + production PlanInput schema",
              "database_writes": 0, "external_calls": 0, "model_calls": 0,
              "source_sha256": hashlib.sha256((ROOT / "frontend/src/App.tsx").read_bytes()).hexdigest(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="chromium")
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN", service_workers="block")

            def guard(route):
                target = urlparse(route.request.url)
                if target.scheme + "://" + target.netloc != base:
                    denied.append(target.path)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            context.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
              const note = document.createElement('div'); note.textContent = '隔离 QA · 内存计划，不执行采集';
              note.style.cssText = 'position:fixed;top:0;right:0;z-index:99999;background:#ffe58f;padding:5px 12px';
              document.body.appendChild(note); });""")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({"path": urlparse(response.url).path, "status": response.status}) if "/api/" in response.url else None)
            page.goto(base + "/plans", wait_until="networkidle")
            result.update(verify(page, fixture, args.output, expect))
            assert not errors and not denied
            assert all(response["status"] == 200 for response in responses)
            assert len(fixture.writes) == 4
            result["passed"] = True
            browser.close()
    except Exception as exc:
        result.update(passed=False, error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        server.shutdown()
        server.server_close()
        result.update(js_errors=errors, denied_requests=denied, api_responses=responses, writes=fixture.writes,
                      search_settings_writes=fixture.search_writes)
        (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": True, "cases": len(result["cases"]), "summary": str(args.output / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
