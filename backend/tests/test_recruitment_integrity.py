"""Recruitment evidence must never turn an incomplete/blocked read into zero hiring."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from app.collection import CollectionError, collect_generic
from app.browser_capture import BrowserCapture
from app.config import get_settings
from app.website_agent import WebsitePlan, collect_with_plan, guarded_page


class Pages:
    def __init__(self, pages):
        self.pages = pages
        self.visited = []

    def fetch(self, url):
        self.visited.append(url)
        return url, self.pages[url]

    def close(self):
        pass


def listing(*jobs, footer='<div class="end">没有更多职位</div>'):
    items = [{"@type": "JobPosting", "identifier": str(number), "title": f"工程师{number}",
              "url": f"/job/{number}", "description": "负责研发与系统维护"} for number in jobs]
    return '<script type="application/ld+json">' + json.dumps(items) + '</script>' + footer


def generic(monkeypatch, html, **filters):
    pages = Pages({"https://jobs.example.com/jobs": html})
    monkeypatch.setattr("app.collection.PublicFetcher", lambda: pages)
    return collect_generic({"entry_url": "https://jobs.example.com/jobs",
                            "config": {"complete_selector": ".end"}},
                           {"max_pages": 2, "max_details": 2, **filters})


def test_jsonld_single_page_respects_detail_budget_and_preserves_partial(monkeypatch):
    result = generic(monkeypatch, listing(1, 2, 3))
    assert len(result.records) == 2
    assert result.coverage == "partial"


def test_generic_marker_presence_is_not_proof_of_complete_coverage(monkeypatch):
    result = generic(monkeypatch, listing(1, footer='<div class="end">加载中</div>'))
    assert result.coverage == "unknown"


def test_generic_login_page_cannot_be_a_complete_zero(monkeypatch):
    with pytest.raises(CollectionError) as exc:
        generic(monkeypatch, '<h1>请先登录</h1><div class="end">没有更多职位</div>')
    assert exc.value.code == "waiting_login"


@pytest.mark.parametrize(("message", "code"), [
    ("请先登录后查看岗位", "waiting_login"),
    ("完成安全验证", "waiting_verification"),
    ("访问过于频繁", "waiting_verification"),
])
def test_login_and_verification_are_distinct_states(message, code):
    with pytest.raises(CollectionError) as exc:
        guarded_page(Pages({"https://jobs.example.com": message}), "https://jobs.example.com")
    assert exc.value.code == code


def test_saved_plan_end_marker_does_not_override_contradicting_total(monkeypatch):
    pages = Pages({
        "https://jobs.example.com/jobs": '<span class="total">共3个职位</span>'
        '<a class="job" href="/job/1">工程师</a><div class="end">没有更多职位</div>',
        "https://jobs.example.com/job/1": '<h1>工程师</h1><div class="desc">负责平台研发</div>',
    })
    monkeypatch.setattr("app.website_agent.PublicFetcher", lambda: pages)
    result = collect_with_plan({"entry_url": "https://jobs.example.com/jobs"},
                               {"max_pages": 2, "max_details": 5}, WebsitePlan(
                                   job_link_selector=".job", title_selector="h1",
                                   description_selector=".desc", total_selector=".total",
                                   complete_selector=".end"))
    assert len(result.records) == 1
    assert result.coverage != "complete"


def test_jsonld_pagination_does_not_leave_company_host(monkeypatch):
    pages = Pages({"https://jobs.example.com/jobs": listing(
        1, footer='<a class="next" href="https://other.example.com/jobs">下一页</a>')})
    monkeypatch.setattr("app.collection.PublicFetcher", lambda: pages)
    result = collect_generic({"entry_url": "https://jobs.example.com/jobs",
                              "config": {"next_selector": ".next"}},
                             {"max_pages": 2, "max_details": 5})
    assert len(result.records) == 1
    assert pages.visited == ["https://jobs.example.com/jobs"]
    assert result.coverage != "complete"


@pytest.mark.parametrize(("status", "code"), [(401, "waiting_login"), (403, "access_restricted")])
def test_rendered_browser_rejects_http_error_even_if_page_has_jobs(monkeypatch, status, code):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(listing(1).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(get_settings(), "allow_local_fixture_fetch", True)
    browser = BrowserCapture()
    try:
        with pytest.raises(CollectionError) as exc:
            browser.fetch(f"http://127.0.0.1:{server.server_port}/jobs")
        assert exc.value.code == code
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
