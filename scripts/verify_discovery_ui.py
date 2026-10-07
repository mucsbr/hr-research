"""Exercise discovery success, partial coverage, no candidates and failure in the built UI.

Uses an isolated localhost fixture only. Search, binding, database and models are never called.
"""
from argparse import ArgumentParser
from copy import deepcopy
from functools import partial
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse

from verify_plan_filters_ui import Handler, ROOT

COMPANY = "00000000-0000-4000-8000-000000000091"
COMPANY_PATH = "/api/v1/companies/" + COMPANY
QUERY = '"QA关注公司" 招聘 官网'
EMPTY_DETAIL = "搜索已完成，但未找到匹配主体的候选；请补充公司官网后再发现入口。"
ERROR_DETAIL = "搜索引擎当前不可用，请稍后重试；未访问任何候选站点。"


def candidate(number, title, search=None):
    return {"id": "00000000-0000-4000-8000-00000000009" + str(number),
            "url": f"https://candidate-{number}.example/jobs", "title": title, "status": "pending",
            "evidence": {"method": "search_result", "identity_basis": "隔离QA，候选尚未核验", **(search or {})}}


class FixtureAPI:
    def __init__(self):
        self.calls, self.discovery_calls = [], 0
        self.candidates = [candidate(2, "此前保留的候选入口")]

    def request(self, method, path, body):
        self.calls.append({"method": method, "path": path, "body": deepcopy(body) if method != "GET" else None})
        if method == "GET":
            if path == "/api/v1/auth/me":
                return 200, {"username": "隔离QA", "role": "admin"}
            if path == COMPANY_PATH:
                return 200, {"id": COMPANY, "name": "QA关注公司", "official_name": "QA关注公司有限公司",
                             "aliases": ["QA品牌"], "website": None, "archived": False, "role": "competitor",
                             "notes": "只存在于隔离浏览器夹具"}
            if path == COMPANY_PATH + "/entry-candidates":
                return 200, self.candidates
            if path in {COMPANY_PATH + "/job-matches", COMPANY_PATH + "/job-merges"}:
                return 200, []
            if path == "/api/v1/jobs":
                return 200, {"items": []}
        if method == "POST" and path == COMPANY_PATH + "/entry-discovery":
            assert body == {}
            self.discovery_calls += 1
            if self.discovery_calls == 4:
                return 409, {"detail": {"code": "search_unavailable", "message": ERROR_DETAIL}}
            search = {"query": QUERY, "request_status": "succeeded", "returned_results": 1,
                      "unresponsive_engines": []}
            status, detail, entries = "candidates_found", "搜索结果仅为候选，未访问或自动绑定任何候选站点。", []
            if self.discovery_calls == 1:
                entries = [candidate(3, "本次成功发现的候选", search)]
            elif self.discovery_calls == 2:
                search.update(request_status="partial", unresponsive_engines=[
                    {"engine": "bing", "reason": "timeout"}, {"engine": "baidu", "reason": "rate_limited"}])
                status, detail = "partial", "部分引擎未响应，已保存可用候选供核对。"
                entries = [candidate(4, "部分搜索保留的候选", search)]
            else:
                search["returned_results"] = 0
                status, detail = "no_candidates", EMPTY_DETAIL
            self.candidates.extend(entries)
            return 200, {"status": status, "detail": detail, "candidates": entries, "search": search}
        return 405, {"detail": "未登记请求已拒绝；隔离夹具不代理真实API"}


class DiscoveryHandler(Handler):
    def dispatch(self):
        if urlparse(self.path).path == "/companies/" + COMPANY:
            self.path = "/index.html"
        return super().dispatch()

    do_GET = dispatch
    do_POST = dispatch


def discover(page, expected_status, expect):
    with page.expect_response(lambda response: response.url.endswith("/entry-discovery") and response.request.method == "POST") as response:
        page.get_by_role("button", name="发现招聘入口", exact=True).click()
    assert response.value.status == expected_status
    expect(page.get_by_role("button", name="发现招聘入口", exact=True)).to_be_enabled()


def verify(page, fixture, folder, expect):
    expect(page.get_by_role("heading", name="QA关注公司", exact=True)).to_be_visible()
    candidates = page.locator("section.panel").filter(has=page.get_by_role("heading", name="候选入口", exact=True))
    expect(candidates.get_by_role("link", name="此前保留的候选入口", exact=True)).to_be_visible()
    discover(page, 200, expect)
    expect(candidates.get_by_text("本次发现 1 个候选入口", exact=True)).to_be_visible()
    expect(candidates.get_by_text("搜索结果仅为候选，未访问或自动绑定任何候选站点。", exact=True)).to_be_visible()
    expect(candidates.get_by_role("link", name="本次成功发现的候选", exact=True)).to_be_visible()
    expect(candidates.locator("tbody tr")).to_have_count(2)
    page.screenshot(path=str(folder / "discovery-success.png"), animations="disabled")

    discover(page, 200, expect)
    expect(candidates.locator(".ant-alert-warning")).to_be_visible()
    expect(candidates.get_by_text("本次发现 1 个候选，部分搜索引擎未响应，结果可能不完整。", exact=True)).to_be_visible()
    expect(candidates.get_by_text("bing：timeout", exact=True)).to_be_visible()
    expect(candidates.get_by_text("baidu：rate_limited", exact=True)).to_be_visible()
    expect(candidates.get_by_text("查询：" + QUERY + " · 搜索返回 1 条结果", exact=True)).to_be_visible()
    expect(candidates.locator("tbody tr")).to_have_count(3)
    page.screenshot(path=str(folder / "discovery-partial.png"), animations="disabled")
    candidates.locator('tr[data-row-key="00000000-0000-4000-8000-000000000094"]').get_by_role("button", name="查看证据", exact=True).click()
    dialog = page.get_by_role("dialog")
    expect(dialog.locator("pre")).to_contain_text('"request_status": "partial"')
    expect(dialog.locator("pre")).to_contain_text('"engine": "bing"')
    expect(dialog.locator("pre")).to_contain_text('"returned_results": 1')
    page.wait_for_function("""() => { const modal = document.querySelector('.ant-modal');
      return modal && getComputedStyle(modal).transform === 'none' && getComputedStyle(modal).opacity === '1'; }""")
    page.screenshot(path=str(folder / "discovery-partial-evidence.png"))
    dialog.locator(".ant-modal-close").click()
    expect(dialog).not_to_be_visible()

    discover(page, 200, expect)
    expect(candidates.locator(".ant-alert-info").get_by_text(EMPTY_DETAIL, exact=True)).to_be_visible()
    expect(candidates.get_by_text("查询：" + QUERY + " · 搜索返回 0 条结果", exact=True)).to_be_visible()
    expect(candidates.locator("tbody tr")).to_have_count(3)
    page.screenshot(path=str(folder / "discovery-no-candidates.png"), animations="disabled")

    before = deepcopy(fixture.candidates)
    discover(page, 409, expect)
    alert = candidates.locator(".ant-alert-error")
    expect(alert.get_by_text("入口发现未完成，保留此前候选。", exact=True)).to_be_visible()
    expect(alert.get_by_text(ERROR_DETAIL, exact=True)).to_be_visible()
    expect(candidates.locator("tbody tr")).to_have_count(3)
    for entry in before:
        expect(candidates.get_by_role("link", name=entry["title"], exact=True)).to_have_attribute("href", entry["url"])
    assert fixture.candidates == before
    page.screenshot(path=str(folder / "discovery-error-preserves-candidates.png"), animations="disabled")
    writes = [call for call in fixture.calls if call["method"] != "GET"]
    assert len(writes) == 4 and all(call["path"] == COMPANY_PATH + "/entry-discovery" for call in writes)
    assert not any("/bind" in call["path"] or "/reject" in call["path"] for call in fixture.calls)
    width = candidates.evaluate("el => ({client: el.clientWidth, scroll: el.scrollWidth})")
    assert width["scroll"] <= width["client"] + 1
    return {"cases": ["success_with_candidate", "partial_candidates_and_engine_failures_with_evidence",
                       "no_candidates_uses_service_detail", "409_keeps_prior_candidates_and_durable_error"],
            "panel_width": width, "binding_requests": 0, "candidate_navigation_requests": 0}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".runtime/discovery-ui-validation-20261005")
    args = parser.parse_args()
    from playwright.sync_api import expect, sync_playwright
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / ".runtime/browsers")
    args.output.mkdir(parents=True, exist_ok=True)
    fixture = FixtureAPI()
    DiscoveryHandler.api = fixture
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DiscoveryHandler, directory=str(ROOT / "frontend/dist")))
    Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:" + str(server.server_port)
    errors, denied, responses = [], [], []
    result = {"scope": "built React UI and isolated localhost discovery responses",
              "database_writes": 0, "external_calls": 0, "model_calls": 0,
              "source_sha256": hashlib.sha256((ROOT / "frontend/src/CompanyWorkspace.tsx").read_bytes()).hexdigest(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, channel="chromium")
            context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN", service_workers="block")

            def guard(route):
                target = urlparse(route.request.url)
                if target.scheme + "://" + target.netloc != base:
                    denied.append(route.request.url)
                    route.abort()
                else:
                    route.continue_()

            context.route("**/*", guard)
            context.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
              const note = document.createElement('div'); note.textContent = '隔离 QA · 未查询真实搜索引擎';
              note.style.cssText = 'position:fixed;top:0;right:0;z-index:99999;background:#ffe58f;padding:5px 12px';
              document.body.appendChild(note); });""")
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({"path": urlparse(response.url).path, "status": response.status}) if "/api/" in response.url else None)
            page.goto(base + "/companies/" + COMPANY, wait_until="networkidle")
            result.update(verify(page, fixture, args.output, expect))
            assert not errors and not denied
            assert len([response for response in responses if response["status"] == 409]) == 1
            assert all(response["status"] in {200, 409} for response in responses)
            result["passed"] = True
            browser.close()
    except Exception as exc:
        result.update(passed=False, error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        server.shutdown()
        server.server_close()
        result.update(js_errors=errors, denied_requests=denied, api_responses=responses, requests=fixture.calls)
        (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": True, "cases": len(result["cases"]), "summary": str(args.output / "summary.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
