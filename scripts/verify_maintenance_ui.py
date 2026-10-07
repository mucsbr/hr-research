"""Read-only live UI review or wholly isolated in-memory API-contract fixtures.

Default invocation prints the plan only. Use --execute after runtime refresh is
confirmed. No model, Runner, collector, notification or database client is used.
"""
from argparse import ArgumentParser
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
NAMES = {"official": "企业官网", "boss": "BOSS 直聘", "liepin": "猎聘", "zhaopin": "智联招聘", "51job": "前程无忧"}
STATES = {"queued": "等待生成", "generating": "生成中", "draft_ready": "候选已生成", "resolving": "锁定依赖",
          "validating": "隔离验证中", "awaiting_publish": "等待发布", "published": "已发布",
          "recovery_queued": "已发布，等待恢复采集", "recovering": "已发布，恢复采集中", "recovered": "已恢复采集",
          "unknown": "结果未知，已停止", "blocked_configuration": "等待运行环境配置",
          "failed": "失败，请查看版本记录", "conflict": "基线版本已变化", "cancelled": "已取消"}
STAMP = "2026-10-05T00:00:00Z"
PROPOSAL = "00000000-0000-4000-8000-000000000010"
CURRENT = "00000000-0000-4000-8000-000000000011"
HISTORY = "00000000-0000-4000-8000-000000000012"
REPAIRED = "00000000-0000-4000-8000-000000000013"


class FixtureAPI:
    """Deliberate UI fixtures, no real backend/DB and no claimed execution proof."""
    def __init__(self):
        self.flags = {kind: {"agent_enabled": False, "auto_repair_enabled": False, "auto_publish_enabled": False,
            "check_enabled": False, "code_agent_enabled": False, "code_auto_publish_enabled": False,
            "code_upstream_enabled": False, "release_generation": 7, "last_check_at": None} for kind in NAMES}
        self.active_id, self.generation = CURRENT, 7
        self.proposal_status, self.override_status = "awaiting_publish", None
        self.reject = {"publish": True, "rollback": True}
        self.calls = []

    def release(self, identifier, status):
        return {"id": identifier, "version": "QA夹具-" + identifier[-2:], "fingerprint": "a" * 64,
                "status": status, "origin": "isolated-ui-fixture", "manifest": {"program": {"contract": "python_adapter_v1"}},
                "validation": {"passed": True, "scope": "QA页面夹具，不是实际执行验收"}, "created_at": STAMP}

    def adapters(self):
        return [{"kind": kind, "registered": True, "upstream_available": kind == "boss",
                 "active": self.release(self.active_id if kind == "boss" else CURRENT, "published") if kind == "boss" else {
                     "id": CURRENT, "version": "QA内置-" + kind, "fingerprint": "b" * 64, "status": "published",
                     "origin": "isolated-ui-fixture", "manifest": {}},
                 "registry": {**flags, "release_generation": self.generation if kind == "boss" else 7}}
                for kind, flags in self.flags.items()]

    def proposal(self):
        return {"id": PROPOSAL, "kind": "boss", "origin": "failure_evidence", "status": self.override_status or self.proposal_status,
                "attempts": 1, "max_candidates": 2, "created_at": STAMP, "base_generation": 7,
                "current_generation": self.generation, "current_active_release_id": self.active_id,
                "release_id": REPAIRED, "bundle_sha": "c" * 64, "validation_count": 3, "completed_validation_count": 3,
                "error_message": "隔离 QA API 夹具，仅验证页面展示与请求，不代表真实发布/恢复。",
                "source_diff": [{"path": "adapter.py", "diff": "--- old/adapter.py\n+++ new/adapter.py\n@@ QA fixture @@\n-old_selector\n+verified_selector\n", "truncated": False},
                                {"path": "lib/rules.py", "diff": "+QA_TRUNCATION_BOUNDARY=" + "long_identifier_" * 50, "truncated": True}],
                "dependency_diff": {"before": [], "after": ["beautifulsoup4==4.13.5"]},
                "validation_run_refs": [{"case_id": op, "status": "succeeded", "run_id": "qa-fixture-" + op,
                                          "verification_outcome": "fixture_only"} for op in ["list", "detail", "navigation"]],
                "task_summary": {"fixture": True, "actual_candidate_execution": False},
                "model_contexts": [{"fixture": True, "model_calls": 0}]}

    def request(self, method, path, body):
        self.calls.append({"method": method, "path": path, "body": body if method != "GET" else None})
        prefix = "/api/v1/adapter-maintenance"
        if method == "GET":
            if path == "/api/v1/auth/me":
                return 200, {"username": "QA夹具", "role": "admin"}
            if path == prefix + "/adapters":
                return 200, self.adapters()
            if path in {prefix + "/runs", prefix + "/incidents"}:
                return 200, []
            if path == prefix + "/code-proposals":
                return 200, [self.proposal()]
            if path == prefix + "/code-proposals/" + PROPOSAL:
                return 200, self.proposal()
            if path == prefix + "/adapters/boss/releases":
                return 200, [self.release(self.active_id, "published"), self.release(HISTORY, "retired")]
        if method == "PATCH" and path == prefix + "/adapters/boss/settings":
            if set(body) - {"code_agent_enabled", "code_auto_publish_enabled", "code_upstream_enabled"}:
                return 400, {"detail": "QA夹具拒绝修改其他开关"}
            self.flags["boss"].update(body)
            if body.get("code_agent_enabled") is False:
                self.flags["boss"].update(code_auto_publish_enabled=False, code_upstream_enabled=False)
            return 200, self.flags["boss"]
        operation = "publish" if path == prefix + "/code-proposals/" + PROPOSAL + "/publish" else "rollback" if path == prefix + "/releases/" + HISTORY + "/rollback" else None
        if method == "POST" and operation:
            expected = {"expected_active_release_id": self.active_id, "expected_generation": self.generation}
            if body != expected:
                return 422, {"detail": "QA夹具：页面CAS请求没有携带读取到的版本与代次"}
            if self.reject[operation]:
                self.reject[operation] = False
                return 409, {"detail": "QA夹具：活动版本或代次已变化，未覆盖并发发布结果"}
            self.active_id = REPAIRED if operation == "publish" else HISTORY
            self.generation += 1
            if operation == "publish":
                self.proposal_status = "recovery_queued"
            return 200, self.release(self.active_id, "published")
        return 405, {"detail": "QA夹具拒绝未登记请求；不会代理到真实API"}


class FixtureHandler(SimpleHTTPRequestHandler):
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
        if path == "/maintenance":
            self.path = "/index.html"
        super().do_GET()

    do_GET = dispatch
    do_POST = dispatch
    do_PATCH = dispatch
    do_DELETE = dispatch


def panel(page, title):
    return page.locator("section.panel").filter(has=page.get_by_role("heading", name=title, exact=True))


def stable_drawer(page):
    page.wait_for_function("""() => [...document.querySelectorAll('.ant-drawer-open .ant-drawer-content-wrapper')]
        .some(el => getComputedStyle(el).transform === 'none' && el.getBoundingClientRect().width > 0)""")
    previous = None
    stable = 0
    for _ in range(20):
        value = page.locator(".ant-drawer-open .ant-drawer-content-wrapper").last.bounding_box()
        stable = stable + 1 if value == previous else 0
        if stable >= 4:
            return
        previous = value
        page.wait_for_timeout(100)
    raise AssertionError("Drawer did not settle before screenshot")


def record_screen(page, folder, name):
    # A full-page capture while a fixed drawer is open misplaces it relative to
    # the scrolled document; capture the actual viewport for overlays.
    overlay = page.locator(".ant-drawer-open, .ant-popconfirm").count() > 0
    page.screenshot(path=str(folder / (name + ".png")), full_page=not overlay)


def verify_common(page, folder, expect, *, fixture):
    expect(page.get_by_role("heading", name="适配器维护", exact=True)).to_be_visible()
    platforms = panel(page, "已支持的招聘平台")
    codes = panel(page, "代码与依赖维护")
    for name in NAMES.values():
        expect(platforms.get_by_role("cell", name=name, exact=True)).to_be_visible()
        expect(codes.get_by_role("cell", name=name, exact=True)).to_be_visible()
    expect(codes.get_by_role("switch")).to_have_count(15)
    record_screen(page, folder, "maintenance-" + ("fixture" if fixture else "live-read-only"))
    return codes


def verify_fixture(page, fixture, folder, expect):
    codes = verify_common(page, folder, expect, fixture=True)
    boss = codes.locator('tr[data-row-key="boss"]')
    switches = boss.get_by_role("switch")
    expect(switches.nth(0)).not_to_be_checked()
    expect(switches.nth(1)).to_be_disabled()
    expect(switches.nth(2)).to_be_disabled()
    switches.nth(0).click()
    expect(switches.nth(0)).to_be_checked()
    expect(switches.nth(1)).to_be_enabled()
    expect(switches.nth(2)).to_be_enabled()
    switches.nth(2).click()
    expect(switches.nth(2)).to_be_checked()
    switches.nth(0).click()
    expect(switches.nth(2)).not_to_be_checked()
    assert not fixture.flags["boss"]["agent_enabled"]
    expect(codes.locator('tr[data-row-key="official"]').get_by_role("switch").nth(1)).to_be_disabled()
    expect(page.locator(".ant-message-notice")).to_have_count(0, timeout=10000)
    proposals = panel(page, "代码维护提案")
    proposals.get_by_role("button", name="差异与验证", exact=True).click()
    stable_drawer(page)
    drawer = page.locator(".ant-drawer-open")
    expect(drawer.get_by_text("基线代次 7 · 当前代次 7")).to_be_visible()
    expect(drawer.get_by_text("候选依赖：beautifulsoup4==4.13.5")).to_be_visible()
    expect(drawer.get_by_text("展示已截断", exact=True)).to_be_visible()
    expect(drawer.get_by_role("row")).to_have_count(4)
    width = drawer.locator(".ant-drawer-body").evaluate("el => ({client: el.clientWidth, scroll: el.scrollWidth})")
    assert width["scroll"] <= width["client"] + 1, width
    record_screen(page, folder, "proposal-detail-fixture")
    drawer.locator(".ant-drawer-close").click()
    expect(page.locator(".ant-drawer-open")).to_have_count(0)
    for expected_status in ["conflict", "success"]:
        expect(page.locator(".ant-message-notice")).to_have_count(0, timeout=10000)
        proposals.get_by_role("button", name=re.compile(r"^发\s*布$")).click()
        with page.expect_response(lambda response: response.url.endswith("/publish") and response.request.method == "POST") as sent:
            page.locator(".ant-popconfirm").get_by_role("button", name=re.compile(r"^确\s*定$")).click()
        assert sent.value.status == (409 if expected_status == "conflict" else 200)
        if expected_status == "conflict":
            expect(page.get_by_text("QA夹具：活动版本或代次已变化，未覆盖并发发布结果", exact=True)).to_be_visible()
            page.wait_for_timeout(350)  # Settle toast/popconfirm transitions before evidence capture.
            record_screen(page, folder, "publish-cas-conflict-fixture")
        else:
            expect(proposals.get_by_text("已发布，等待恢复采集", exact=True)).to_be_visible()
    platform_boss = panel(page, "已支持的招聘平台").locator('tr[data-row-key="boss"]')
    platform_boss.get_by_role("button", name="版本管理", exact=True).click()
    stable_drawer(page)
    for expected_status in ["conflict", "success"]:
        expect(page.locator(".ant-message-notice")).to_have_count(0, timeout=10000)
        page.locator(".ant-drawer-open").get_by_role("button", name="回滚到此版", exact=True).click()
        with page.expect_response(lambda response: response.url.endswith("/rollback") and response.request.method == "POST") as sent:
            page.locator(".ant-popconfirm").get_by_role("button", name=re.compile(r"^确\s*定$")).click()
        assert sent.value.status == (409 if expected_status == "conflict" else 200)
        if expected_status == "conflict":
            expect(page.get_by_text("QA夹具：活动版本或代次已变化，未覆盖并发发布结果", exact=True)).to_be_visible()
            page.wait_for_timeout(350)
            record_screen(page, folder, "rollback-cas-conflict-fixture")
        else:
            expect(page.locator(".ant-drawer-open").get_by_role("button", name="回滚到此版", exact=True)).to_have_count(0)
    page.locator(".ant-drawer-open .ant-drawer-close").click()
    for state, label in STATES.items():
        fixture.override_status = state
        page.reload()
        row = panel(page, "代码维护提案").locator('tr[data-row-key="' + PROPOSAL + '"]')
        expect(row.get_by_text(label, exact=True)).to_be_visible()
        if state in {"published", "recovery_queued", "recovering", "recovered", "unknown", "failed", "conflict", "cancelled"}:
            expect(row.get_by_role("button", name=re.compile(r"^发\s*布$"))).to_have_count(0)
        if state == "failed":
            assert "保留旧版" not in row.inner_text()
            record_screen(page, folder, "published-recovery-failed-neutral-fixture")
    assert all(call["path"].startswith("/api/v1/adapter-maintenance") or call["path"] == "/api/v1/auth/me" for call in fixture.calls)
    return {"state_labels": STATES, "fixture_requests": fixture.calls, "drawer_width": width,
            "cas_scope": "browser request payload and simulated 409 display only; real DB CAS is separately tested"}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fixture", "live-read-only"], default="fixture")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime" / "maintenance-ui-validation-20261005")
    args = parser.parse_args()
    plan = {"mode": args.mode, "executed": False, "sources": NAMES, "states": STATES,
            "fixture_storage": "isolated in-memory API contract; no database or real backend",
            "live_scope": "login session plus GET only; no settings, publishing, rollback or other business writes",
            "external_calls": 0, "model_calls": 0, "candidate_execution": False}
    if not args.execute:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return
    from app.config import get_settings
    from playwright.sync_api import expect, sync_playwright
    settings = get_settings()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(settings.browser_cache_dir)
    folder = args.output / args.mode
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    fixture, server = None, None
    base_url = args.base_url.rstrip("/")
    if args.mode == "fixture":
        if not (ROOT / "frontend/dist/index.html").is_file():
            raise SystemExit("Build the current frontend before fixture review")
        fixture = FixtureAPI()
        FixtureHandler.api = fixture
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FixtureHandler, directory=str(ROOT / "frontend/dist")))
        Thread(target=server.serve_forever, daemon=True).start()
        base_url = "http://127.0.0.1:" + str(server.server_port)
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.username or parsed.password:
        raise SystemExit("Only an explicit local HTTP UI origin is allowed")
    errors, blocked, api_responses = [], [], []
    result = {**plan, "executed": True, "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        with sync_playwright() as playwright:
            # Reuse the project's installed full Chromium; no browser download.
            browser = playwright.chromium.launch(headless=True, channel="chromium")
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN", timezone_id="Asia/Shanghai", service_workers="block")
            if fixture is None:
                response = context.request.post(base_url + "/api/v1/auth/login", data={
                    "username": settings.admin_username, "password": settings.admin_password.get_secret_value()})
                if response.status != 200:
                    raise AssertionError("Local UI login failed; credentials were not logged")
            def guard(route):
                request = route.request
                target = urlparse(request.url)
                if target.scheme + "://" + target.netloc != parsed.scheme + "://" + parsed.netloc or (fixture is None and request.method != "GET"):
                    blocked.append({"method": request.method, "path": target.path})
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", guard)
            if fixture is not None:
                context.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
                    const label = document.createElement('div'); label.textContent = '隔离 QA API 夹具 · 非真实模型、采集、发布';
                    label.style.cssText = 'position:fixed;top:0;right:0;z-index:99999;background:#ffe58f;padding:4px 12px;font:12px sans-serif';
                    document.body.appendChild(label); });""")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: api_responses.append({"path": urlparse(response.url).path, "status": response.status}) if "/api/" in response.url else None)
            page.goto(base_url + "/maintenance", wait_until="networkidle")
            if fixture is not None:
                result.update(verify_fixture(page, fixture, folder, expect))
            else:
                verify_common(page, folder, expect, fixture=False)
                codes = panel(page, "代码与依赖维护")
                result["switches_read_only"] = [{"kind": kind, "checked": codes.locator('tr[data-row-key="' + kind + '"]').get_by_role("switch").evaluate_all("els => els.map(el => el.getAttribute('aria-checked') === 'true')")} for kind in NAMES]
                versions = panel(page, "已支持的招聘平台").locator('tr[data-row-key="boss"]')
                versions.get_by_role("button", name="版本管理", exact=True).click()
                stable_drawer(page)
                record_screen(page, folder, "versions-live-read-only")
                page.locator(".ant-drawer-open .ant-drawer-close").click()
                details = panel(page, "代码维护提案").get_by_role("button", name="差异与验证", exact=True)
                if details.count():
                    details.first.click()
                    stable_drawer(page)
                    record_screen(page, folder, "existing-proposal-live-read-only")
                result["real_proposals_present"] = bool(details.count())
            result.update(js_errors=errors, blocked_requests=blocked, api_responses=api_responses)
            assert not errors and not blocked, "UI JavaScript error or unapproved request; inspect private summary"
            assert not any(item["status"] >= 400 and not (fixture and item["status"] == 409) for item in api_responses), "Unexpected API error"
            result["passed"] = True
            browser.close()
    except Exception as exc:
        result.update(passed=False, failure_type=type(exc).__name__, failure=str(exc), js_errors=errors,
                      blocked_requests=blocked, api_responses=api_responses)
        if fixture:
            result["fixture_requests"] = fixture.calls
        raise
    finally:
        if server:
            server.shutdown()
            server.server_close()
        (folder / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"mode": args.mode, "passed": result["passed"], "summary": str(folder / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
