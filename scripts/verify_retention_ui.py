"""Verify built retention UI against an isolated HTTP fixture and production input schemas.

Dry by default; --execute uses only localhost, in-memory records, and Chromium.
No database, deletion worker, remote API, account, model or notification is used.
"""
from argparse import ArgumentParser
from copy import deepcopy
from datetime import datetime, timedelta, timezone
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
CATEGORIES = ["raw_documents", "reports", "task_logs", "expired_sessions"]
NAMES = ["原始网页与资料", "报告正文与文件", "任务运行日志", "过期登录会话"]
USER_ID = "00000000-0000-4000-8000-000000000034"
CLEARED = "2026-10-05T00:15:00Z"
STORAGE_KEY = "hr-retention-intent:" + USER_ID
CLOCK = datetime(2026, 10, 5, 0, 20, tzinfo=timezone.utc)


def stamp(days=0, seconds=0):
    return (CLOCK + timedelta(days=days, seconds=seconds)).isoformat()


class FixtureAPI:
    def __init__(self):
        self.role = "admin"
        self.calls, self.writes, self.errors = [], [], []
        self.preview_mode, self.execute_mode = "grace", "unknown"
        self.lookup_visible = False
        self.previews, self.runs = {}, {}
        self.policies = [{"category": category, "retention_days": days, "enabled": True,
                          "revision": 1, "grace_until": stamp(days=7), "updated_at": stamp()}
                         for category, days in zip(CATEGORIES, [90, 180, 90, 7], strict=True)]
        self.reports = [{"id": str(uuid.uuid4()), "title": "QA已清理招聘报告", "domain": "recruitment",
                         "content": "", "model": "mock-only", "created_at": stamp(days=-200),
                         "cleared_at": CLEARED, "retention": {"reason": "retention_policy"},
                         "evidence": [{"title": "原岗位证据", "source_url": "https://example.invalid/job/34"}],
                         "coverage": {}},
                        {"id": str(uuid.uuid4()), "title": "QA暂无正文报告", "domain": "recruitment",
                         "content": "", "model": None, "created_at": "2026-10-05", "cleared_at": None,
                         "evidence": [], "coverage": {}}]
        self.sentiment_reports = [{"id": str(uuid.uuid4()), "title": "QA待清理BettaFish报告",
                                  "domain": "sentiment", "content": "QA旧正文仅为内存夹具", "model": "mock-only",
                                  "created_at": stamp(days=-200), "cleared_at": None,
                                  "evidence": [{"title": "原舆情证据", "url": "https://example.invalid/post/34"}],
                                  "coverage": {"engine": "bettafish"}}]

    def envelope(self):
        return {"policies": deepcopy(self.policies), "can_manage": self.role == "admin",
                "limits": {"retention_days_min": 1, "retention_days_max": 3650,
                           "batch_size_max": 200, "preview_ttl_seconds": 900},
                "scheduler": {"enabled": any(item["enabled"] for item in self.policies),
                              "batch_size": 100, "max_seconds": 30},
                "protected": ["公司、岗位版本、任务和模型用量保留。", "隔离 QA 夹具，不是真实清理。"]}

    def request(self, method, full_path, body, headers):
        from app.retention_api import PolicyUpdate, PreviewInput, RunInput
        parsed = urlparse(full_path)
        path, query = parsed.path, parse_qs(parsed.query)
        call = {"method": method, "path": path, "query": query, "body": deepcopy(body),
                "idempotency_key": headers.get("Idempotency-Key")}
        self.calls.append(call)
        if method == "GET":
            if path == "/api/v1/auth/me":
                return 200, {"id": USER_ID, "username": "隔离QA", "role": self.role}
            if path == "/api/v1/system/status":
                return 200, {"database": "ready", "timezone": "Asia/Shanghai",
                             "llm": {"configured": False, "model": "", "daily_request_limit": 0,
                                     "task_call_limit": 0, "max_output_tokens": 0, "calls_today": 0}}
            if path == "/api/v1/sentiment/status":
                return 200, {"bettafish": {"status": "blocked_configuration", "reason": "隔离夹具，未连接模型"},
                             "limitations": []}
            if path == "/api/v1/settings/recruitment-discovery":
                assert self.role == "admin", "Ordinary user requested admin-only search settings"
                return 200, {"configured": False, "config": None}
            if path in {"/api/v1/users", "/api/v1/audit-events"}:
                assert self.role == "admin", "Ordinary user requested admin-only users/audit"
                return 200, []
            if path == "/api/v1/retention/policies":
                return 200, self.envelope()
            if path == "/api/v1/retention/runs":
                return 200, list(self.runs.values())
            if path == "/api/v1/retention/runs/by-key":
                value = self.runs.get(query.get("idempotency_key", [""])[0])
                if self.lookup_visible and value:
                    return 200, value
                return 404, {"detail": "夹具模拟登记结果暂不可见"}
            if path.startswith("/api/v1/retention/runs/"):
                return 200, next(value for value in self.runs.values() if value["id"] == path.rsplit("/", 1)[1])
            if path == "/api/v1/reports":
                return 200, self.reports
            if path == "/api/v1/sentiment/reports":
                return 200, self.sentiment_reports
            if path == "/api/v1/sentiment/items":
                return 200, {"items": [], "total": 0}
            if path in {"/api/v1/sentiment/subjects", "/api/v1/sentiment/sources",
                        "/api/v1/sentiment/runs", "/api/v1/source-accounts"}:
                return 200, []
        assert self.role == "admin", "Ordinary user attempted a write"
        if method == "PUT" and path == "/api/v1/retention/policies":
            data = PolicyUpdate.model_validate(body).model_dump(mode="json")
            for item in data["policies"]:
                policy = next(value for value in self.policies if value["category"] == item["category"])
                assert item["expected_revision"] == policy["revision"]
                policy.update(retention_days=item["retention_days"], enabled=item["enabled"],
                              revision=policy["revision"] + 1, updated_at=stamp())
            self.writes.append({**call, "validated": data})
            return 200, self.envelope()
        if method == "POST" and path == "/api/v1/retention/preview":
            data = PreviewInput.model_validate(body).model_dump(mode="json")
            identifier = str(uuid.uuid4())
            ready = self.preview_mode in {"ready", "expired"}
            preview = {"id": identifier, "expires_at": stamp(seconds=-1 if self.preview_mode == "expired" else 900),
                       "can_execute": ready, "eligible_count": 2 if ready else 0,
                       "estimated_bytes": 2048 if ready else 0,
                       "categories": [{"category": category, "retention_days": days,
                                       "cutoff_at": stamp(days=-days), "grace_until": None if ready else stamp(days=7),
                                       "state": "ready" if ready else "disabled" if index == 3 else "grace_period",
                                       "eligible_count": 2 if ready and index == 0 else 0, "has_more": ready and index == 0}
                                      for index, (category, days) in enumerate(zip(CATEGORIES, [120, 180, 90, 7], strict=True))],
                       "items": [{"category": "raw_documents", "target_id": str(uuid.uuid4()),
                                  "title": "QA原始资料", "file_state": "present"}] if ready else [],
                       "limitations": ["本批候选计数不是全库总量；清理时重新核对引用及活动任务。"]}
            self.previews[identifier] = preview
            self.writes.append({**call, "validated": data})
            return 200, preview
        if method == "POST" and path == "/api/v1/retention/runs":
            data = RunInput.model_validate(body).model_dump(mode="json")
            assert headers.get("Idempotency-Key") == data["idempotency_key"]
            self.writes.append({**call, "validated": data})
            if self.execute_mode == "conflict":
                return 409, {"detail": "预览对应策略已修订"}
            existing = self.runs.get(data["idempotency_key"])
            if existing:
                return (202, existing) if existing["preview_id"] == data["preview_id"] else (409, {"detail": "幂等键载荷冲突"})
            run = {"id": str(uuid.uuid4()), "status": "queued", "trigger": "manual", **data,
                   "created_at": stamp(), "started_at": None, "finished_at": None, "summary": {},
                   "items": [], "error_message": None}
            self.runs[data["idempotency_key"]] = run
            return (500, {"detail": "夹具模拟服务器已登记后响应丢失"}) if self.execute_mode == "unknown" else (202, run)
        self.errors.append(call)
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
            try:
                status, value = self.api.request(self.command, self.path, body, self.headers)
            except Exception as error:
                self.api.errors.append({"path": path, "error": str(error)})
                status, value = 500, {"detail": str(error)}
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
        if path in {"/settings", "/reports", "/sentiment"}:
            self.path = "/index.html"
        super().do_GET()

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch


def panel(page, title):
    return page.locator("section.panel").filter(has=page.get_by_role("heading", name=title, exact=True))


def screen(page, folder, name, target=None):
    if page.locator(".ant-message-notice").count():
        page.locator(".ant-message-notice").last.wait_for(state="hidden")
    if target is not None:
        target.scroll_into_view_if_needed()
    # Drawer content becomes "visible" before its horizontal entry transition
    # ends. Capture the settled panel, rather than a narrow edge of the drawer.
    wrappers = page.locator(".ant-drawer-content-wrapper:visible")
    if wrappers.count():
        wrapper = wrappers.last
        page.wait_for_function("element => getComputedStyle(element).transform === 'none'",
                               arg=wrapper.element_handle())
    page.screenshot(path=str(folder / (name + ".png")), animations="disabled")


def request_count(fixture, method, suffix):
    return len([call for call in fixture.calls if call["method"] == method and call["path"] == "/api/v1/retention/" + suffix])


def verify(page, fixture, base, folder, expect):
    cases = []
    expect(page.locator(".header-user").get_by_text("2026 年 10 月 5 日", exact=True)).to_be_visible()
    retention = panel(page, "分类数据保留")
    expect(retention.get_by_role("button", name="修改保留策略")).to_be_visible()
    for category, days in zip(CATEGORIES, [90, 180, 90, 7], strict=True):
        row = retention.locator(f'tr[data-row-key="{category}"]')
        expect(row.get_by_text(f"{days} 天", exact=True)).to_be_visible()
        expect(row.get_by_text("已启用", exact=True)).to_be_visible()
    expect(retention.get_by_text("宽限截至（北京时间）", exact=True)).to_be_visible()
    screen(page, folder, "default-policies", retention)
    cases.append("four_enabled_finite_defaults_and_visible_seven_day_grace")

    retention.get_by_role("button", name="修改保留策略").click()
    dialog = page.get_by_role("dialog", name="配置分类保留期限")
    for category, days in zip(CATEGORIES, [90, 180, 90, 7], strict=True):
        expect(dialog.locator(f'#{category}_retention_days')).to_have_value(str(days))
    writes_before = len(fixture.writes)
    for invalid in ["0", "3651", "1.5", "1e2", "-1", "abc", ""]:
        dialog.locator("#raw_documents_retention_days").fill(invalid)
        dialog.get_by_role("button", name="保存保留策略").click()
        expect(dialog.get_by_text("请输入 1 至 3650 的整数", exact=True)).to_be_visible()
        assert len(fixture.writes) == writes_before
    dialog.locator("#raw_documents_retention_days").fill("120")
    with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/retention/policies")):
        dialog.get_by_role("button", name="保存保留策略").click()
    expect(dialog).not_to_be_visible()
    assert fixture.writes[-1]["body"] == {"policies": [{"category": "raw_documents", "retention_days": 120,
                                                        "enabled": True, "expected_revision": 1}]}
    cases.append("strict_integer_validation_never_submits_old_value_and_only_changed_revision_sent")
    retention.get_by_role("button", name="修改保留策略").click()
    expect(dialog.locator("#raw_documents_retention_days")).to_have_value("120")
    writes_before = len(fixture.writes)
    dialog.get_by_role("button", name="保存保留策略").click()
    expect(dialog).not_to_be_visible()
    assert len(fixture.writes) == writes_before
    cases.append("unchanged_policy_save_has_no_write")

    preview_button = retention.get_by_role("button", name="预览本批影响")
    for invalid in ["0", "201", "1.5", "1e2", "-1", "abc", ""]:
        retention.locator("#limit").fill(invalid)
        preview_button.click()
        expect(retention.get_by_text("请输入 1 至 200 的整数", exact=True)).to_be_visible()
        assert len(fixture.writes) == writes_before
    retention.locator("#limit").fill("100")
    for name in NAMES:
        retention.get_by_role("checkbox", name=name, exact=True).uncheck()
    preview_button.click()
    expect(retention.get_by_text("至少选择一个分类", exact=True)).to_be_visible()
    assert len(fixture.writes) == writes_before
    for name in NAMES:
        retention.get_by_role("checkbox", name=name, exact=True).check()
    preview_button.click()
    preview = panel(page, "本批清理预览")
    expect(preview.get_by_role("button", name="确认执行本批")).to_be_disabled()
    expect(preview.get_by_text("宽限中", exact=True)).to_have_count(3)
    expect(preview.get_by_text("已停用", exact=True)).to_be_visible()
    cases.append("preview_rejects_invalid_limit_and_empty_categories_grace_disabled_not_executable")

    fixture.preview_mode = "ready"
    preview_button.click()
    execute = preview.get_by_role("button", name="确认执行本批")
    expect(execute).to_be_enabled()
    expect(preview.get_by_text("本批可清理", exact=True)).to_be_visible()
    expect(preview.get_by_text("有，未计入本批", exact=True)).to_be_visible()
    expect(preview.get_by_text("本批候选计数不是全库总量；清理时重新核对引用及活动任务。", exact=True)).to_be_visible()
    screen(page, folder, "bounded-preview", preview)
    execute.click()
    confirm = page.get_by_role("dialog", name="确认清理本批历史资料")
    expect(confirm.get_by_text("清理完成后不能在平台内恢复正文或文件，请核对预览与需要保留的资料。运行结果以服务器记录为准。", exact=True)).to_be_visible()
    assert request_count(fixture, "POST", "runs") == 0
    confirm.get_by_role("button", name=re.compile(r"^取\s*消$")).click()
    expect(confirm).not_to_be_visible()
    assert request_count(fixture, "POST", "runs") == 0
    cases.append("bounded_batch_preview_requires_explicit_confirmation_cancel_does_not_execute")

    execute.click()
    confirm.get_by_role("button", name="执行已预览清理").evaluate("el => { el.click(); el.click(); }")
    expect(retention.get_by_text("清理结果未知，原请求标识已保留", exact=True)).to_be_visible()
    assert request_count(fixture, "POST", "runs") == 1
    pending = json.loads(page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY))
    posted = fixture.writes[-1]["body"]
    assert pending["preview_id"] == posted["preview_id"] and pending["idempotency_key"] == posted["idempotency_key"]
    page.reload(wait_until="networkidle")
    expect(retention.get_by_text("清理结果未知，原请求标识已保留", exact=True)).to_be_visible()
    retention.get_by_role("button", name="查询清理结果").click()
    expect(retention.get_by_text("暂未找到原请求记录，仍保留该标识；请稍后继续查询，不要新建重复请求。", exact=True)).to_be_visible()
    assert request_count(fixture, "POST", "runs") == 1
    assert json.loads(page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY)) == pending
    screen(page, folder, "unknown-result-preserved", retention)
    fixture.lookup_visible = True
    run = fixture.runs[pending["idempotency_key"]]
    run.update(status="partial", started_at=stamp(), finished_at=stamp(),
               summary={"selected": 2, "cleared": 1, "skipped": 0, "failed": 1,
                        "files_deleted": 1, "bytes_deleted": 1024},
               items=[{"category": "raw_documents", "target_id": "QA-1", "status": "cleared", "reason": "retention_policy"},
                      {"category": "reports", "target_id": "QA-2", "status": "failed", "reason": "fixture_file_failure"}])
    retention.get_by_role("button", name="查询清理结果").click()
    drawer = page.get_by_role("dialog", name="数据清理运行详情")
    expect(drawer.get_by_text("部分完成", exact=True)).to_be_visible()
    expect(drawer.get_by_text("fixture_file_failure", exact=True)).to_be_visible()
    assert page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY) is None
    assert request_count(fixture, "POST", "runs") == 1
    screen(page, folder, "partial-result", drawer)
    drawer.locator(".ant-drawer-close").click()
    cases.append("double_click_one_request_unknown_500_reload_404_preserve_key_only_get_lookup_partial_result")

    fixture.execute_mode = "conflict"
    preview_button.click()
    expect(execute).to_be_enabled()
    execute.click()
    confirm.get_by_role("button", name="执行已预览清理").click()
    expect(retention.get_by_text("预览对应策略已修订；未继续执行，请重新预览。", exact=True)).to_be_visible()
    assert page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY) is None
    expect(preview).not_to_be_visible()
    cases.append("explicit_409_rejects_old_preview_without_claiming_success")

    fixture.execute_mode = "accepted"
    preview_button.click()
    expect(execute).to_be_enabled()
    execute.click()
    confirm.get_by_role("button", name="执行已预览清理").click()
    expect(drawer.get_by_text("排队中", exact=True)).to_be_visible()
    accepted = json.loads(page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY))
    assert accepted["run_id"] == fixture.runs[accepted["idempotency_key"]]["id"]
    assert request_count(fixture, "POST", "runs") == 3
    drawer.locator(".ant-drawer-close").click()
    expect(retention.get_by_text("已有清理请求等待核对运行结果", exact=True)).to_be_visible()
    expect(preview_button).to_be_disabled()
    fixture.runs[accepted["idempotency_key"]].update(status="succeeded", finished_at=stamp(),
                                                   summary={"selected": 2, "cleared": 2, "skipped": 0,
                                                            "failed": 0, "files_deleted": 0, "bytes_deleted": 0})
    retention.get_by_role("button", name="查询清理结果").click()
    expect(drawer.get_by_text("完成", exact=True)).to_be_visible()
    assert page.evaluate("key => sessionStorage.getItem(key)", STORAGE_KEY) is None
    assert request_count(fixture, "POST", "runs") == 3
    drawer.locator(".ant-drawer-close").click()
    cases.append("202_queued_is_only_registered_pending_intent_blocks_new_run_until_get_confirms_terminal")

    fixture.preview_mode = "expired"
    preview_button.click()
    expect(preview.get_by_text("此预览已过期，请重新预览后再执行。", exact=True)).to_be_visible()
    expect(execute).to_be_disabled()
    assert request_count(fixture, "POST", "runs") == 3
    cases.append("expired_preview_is_not_executable")

    fixture.role = "user"
    before = len(fixture.calls)
    page.reload(wait_until="networkidle")
    expect(retention.get_by_text("当前为只读查看；仅管理员可调整保留策略、预览和执行清理。", exact=True)).to_be_visible()
    for name in ["修改保留策略", "预览本批影响", "确认执行本批"]:
        expect(page.get_by_role("button", name=name, exact=True)).to_have_count(0)
    screen(page, folder, "ordinary-user-readonly", retention)
    assert all(call["method"] == "GET" for call in fixture.calls[before:])
    cases.append("ordinary_user_only_reads_policies_and_runs_no_admin_requests_or_actions")

    page.goto(base + "/reports", wait_until="networkidle")
    row = page.locator(f'tr[data-row-key="{fixture.reports[0]["id"]}"]')
    observed = datetime.fromisoformat(fixture.reports[0]["created_at"]).astimezone(timezone(timedelta(hours=8)))
    expect(row.get_by_text(observed.strftime("%m-%d %H:%M"), exact=True)).to_be_visible()
    expect(page.locator(f'tr[data-row-key="{fixture.reports[1]["id"]}"]').get_by_text("2026-10-05", exact=True)).to_be_visible()
    expect(row.get_by_text("正文已按保留策略清理", exact=True)).to_be_visible()
    expect(row.get_by_text("2026-10-05 08:15（北京时间）", exact=True)).to_be_visible()
    screen(page, folder, "cleared-report-list")
    row.get_by_role("button", name="QA已清理招聘报告").click()
    report = page.get_by_role("dialog", name="QA已清理招聘报告")
    expect(report.get_by_text("正文已按保留策略清理", exact=True)).to_be_visible()
    expect(report.get_by_text("清理时间（北京时间）：2026-10-05 08:15。报告记录与来源链接仍可查看。", exact=True)).to_be_visible()
    expect(report.get_by_role("link", name="原岗位证据")).to_have_attribute("href", "https://example.invalid/job/34")
    screen(page, folder, "cleared-report", report)
    report.locator(".ant-drawer-close").click()
    page.get_by_role("button", name="QA暂无正文报告").click()
    expect(page.get_by_role("dialog", name="QA暂无正文报告").get_by_text("报告暂无可显示的正文，请查看保留的来源证据。", exact=True)).to_be_visible()
    cases.append("cleared_report_list_and_drawer_keep_timestamp_and_source_empty_report_explained")

    page.goto(base + "/sentiment", wait_until="networkidle")
    page.get_by_role("tab", name="舆情报告", exact=True).click()
    page.get_by_role("button", name="QA待清理BettaFish报告").click()
    sentiment = page.get_by_role("dialog", name="QA待清理BettaFish报告")
    expect(sentiment.get_by_text("QA旧正文仅为内存夹具", exact=True)).to_be_visible()
    expect(sentiment.get_by_role("link", name="下载 BettaFish 报告")).to_be_visible()
    fixture.sentiment_reports[0].update(content="", cleared_at=CLEARED)
    expect(sentiment.get_by_text("正文已按保留策略清理", exact=True)).to_be_visible(timeout=15000)
    expect(sentiment.get_by_text("QA旧正文仅为内存夹具", exact=True)).to_have_count(0)
    expect(sentiment.get_by_role("link", name="下载 BettaFish 报告")).to_have_count(0)
    expect(sentiment.get_by_role("link", name="原舆情证据")).to_have_attribute("href", "https://example.invalid/post/34")
    row = page.locator(f'tr[data-row-key="{fixture.sentiment_reports[0]["id"]}"]')
    expect(row.get_by_text(observed.strftime("%Y-%m-%d %H:%M"), exact=True)).to_be_visible()
    expect(row.get_by_text("2026-10-05 08:15（北京时间）", exact=True)).to_be_visible()
    screen(page, folder, "cleared-bettafish-live-update", sentiment)
    cases.append("open_bettafish_drawer_refreshes_tombstone_no_stale_body_or_download_source_preserved")
    cases.append("new_york_browser_fixed_instant_header_shared_dates_and_retention_show_beijing_date_only_keeps_precision")
    return {"cases": cases}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/retention-ui-validation-20261005")
    args = parser.parse_args()
    if not args.execute:
        print("Dry run: --execute tests built frontend through isolated localhost fixture; no real cleanup.")
        return
    from playwright.sync_api import expect, sync_playwright
    if not (ROOT / "frontend/dist/index.html").is_file():
        raise SystemExit("Build the frontend first")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/browsers")
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = FixtureAPI()
    Handler.api = fixture
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT / "frontend/dist")))
    Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_port)
    errors, denied, responses = [], [], []
    source_paths = ["frontend/src/RetentionSettings.tsx", "frontend/src/App.tsx", "frontend/src/api.ts",
                    "frontend/src/SentimentPage.tsx", "frontend/src/ModuleUI.tsx", "frontend/src/date.ts",
                    "backend/app/retention_api.py", "scripts/verify_retention_ui.py"]
    hashes = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in source_paths}
    result = {"scope": "built React UI, localhost fixture, production PolicyUpdate/PreviewInput/RunInput",
              "real_database_writes": 0, "real_deletions": 0, "external_calls": 0, "model_calls": 0,
              "source_sha256": hashes, "browser_timezone": "America/New_York", "clock": CLOCK.isoformat()}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="chromium")
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN",
                                          timezone_id="America/New_York", service_workers="block")

            def guard(route):
                target = urlparse(route.request.url)
                if target.scheme + "://" + target.netloc != base:
                    denied.append(target.path)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            context.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
              const note = document.createElement('div'); note.textContent = '隔离 QA 夹具，不是真实清理';
              note.style.cssText = 'position:fixed;top:0;right:0;z-index:99999;background:#ffe58f;padding:5px 12px';
              document.body.appendChild(note); });""")
            page = context.new_page()
            page.clock.set_fixed_time(CLOCK)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({"path": urlparse(response.url).path,
                                                                    "method": response.request.method,
                                                                    "status": response.status}) if "/api/" in response.url else None)
            page.goto(base + "/settings", wait_until="networkidle")
            result.update(verify(page, fixture, base, args.output, expect))
            assert not errors and not denied and not fixture.errors, (errors, denied, fixture.errors)
            abnormal = [response for response in responses if response["status"] >= 400]
            assert abnormal == [{"path": "/api/v1/retention/runs", "method": "POST", "status": 500},
                                {"path": "/api/v1/retention/runs/by-key", "method": "GET", "status": 404},
                                {"path": "/api/v1/retention/runs", "method": "POST", "status": 409}], abnormal
            assert all(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest for path, digest in hashes.items()), "Source changed during verification"
            result["passed"] = True
            browser.close()
    except Exception as error:
        result.update(passed=False, error=str(error), error_type=type(error).__name__)
        raise
    finally:
        server.shutdown()
        server.server_close()
        result.update(js_errors=errors, denied_requests=denied, api_responses=responses,
                      memory_writes=fixture.writes, fixture_errors=fixture.errors)
        (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": True, "cases": len(result["cases"]), "summary": str(args.output / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
