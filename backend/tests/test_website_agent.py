import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx
import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError
from sqlalchemy import select

from app.config import get_settings
from app.llm import ModelGateway
from app.models import Company, ModelCall, SourceBinding, Subject, TaskRun
from app.serialization import row
from app.website_agent import WebsitePlan, collect_official, declared_total, eligible_links


@pytest.fixture
def website(monkeypatch):
    state = {"layout": "old", "dynamic": False}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            cls = "job-link" if state["layout"] == "old" else "vacancy"
            if self.path == "/jobs":
                content = (
                    '<nav><a href="/privacy">隐私政策</a></nav>'
                    '<main><div class="total">共2个职位</div>'
                    f'<a class="{cls}" href="/job/1">Python工程师</a>'
                    f'<a class="{cls}" href="/job/2">产品经理</a>'
                    '<div class="end">没有更多职位</div></main>'
                )
                body = (
                    '<html><body><div id="root"></div><script>'
                    'document.getElementById("root").innerHTML=' + json.dumps(content) +
                    ';</script></body></html>'
                ) if state["dynamic"] else "<html><body>" + content + "</body></html>"
            elif self.path.startswith("/job/"):
                title = "Python工程师" if self.path.endswith("1") else "产品经理"
                body = (
                    f'<html><body><h1 class="job-title">{title}</h1>'
                    '<div class="city">上海</div><section class="description">'
                    + "负责平台研发与团队协作，维护产品功能，关注服务稳定性。" * 8
                    + '</section><section class="requirements">具有相关工作经验，熟悉研发流程。</section>'
                    '</body></html>'
                )
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(get_settings(), "allow_local_fixture_fetch", True)
    yield f"http://127.0.0.1:{server.server_port}/jobs", state
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def seed(db, url):
    subject = Subject(name="本地官网测试")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name=subject.name)
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="careers", entry_url=url)
    db.add(binding)
    task = TaskRun(company_id=company.id, config_snapshot={}, scope_hash="test")
    db.add(task)
    db.commit()
    return binding, task


def mock_planner(monkeypatch, calls):
    def handle(request):
        data = json.loads(request.content)
        page = json.loads(data["messages"][-1]["content"])
        calls.append(page)
        selector = ".vacancy" if 'class="vacancy"' in page["listing_html"] else ".job-link"
        plan = {
            "render_mode": page["render_mode"],
            "job_link_selector": selector,
            "title_selector": ".job-title",
            "description_selector": ".description",
            "requirements_selector": ".requirements",
            "location_selector": ".city",
            "total_selector": ".total",
            "complete_selector": ".end",
        }
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(plan)}, "finish_reason": "stop"}
        ], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    monkeypatch.setattr("app.website_agent.ModelGateway", lambda: ModelGateway(httpx.MockTransport(handle)))


def test_rule_is_learned_reused_and_repaired_after_layout_change(db, website, monkeypatch):
    url, website_state = website
    binding, task = seed(db, url)
    calls = []
    mock_planner(monkeypatch, calls)
    result = collect_official(row(binding), {"max_pages": 2, "max_details": 5}, task.id)
    assert len(result.records) == 2 and result.coverage == "complete"
    db.refresh(binding)
    first_hash = binding.config["plan_hash"]
    assert result.adapter_fingerprint == first_hash
    reused = collect_official(row(binding), {"max_pages": 2, "max_details": 5}, task.id)
    assert reused.adapter_fingerprint == first_hash
    assert len(calls) == 1
    website_state["layout"] = "new"
    result = collect_official(row(binding), {"max_pages": 2, "max_details": 5}, task.id)
    db.refresh(binding)
    assert len(result.records) == 2 and binding.config["plan_hash"] != first_hash
    assert len(calls) == 2
    assert len(db.scalars(select(ModelCall)).all()) == 2


def test_dynamic_listing_uses_project_chromium_and_mock_model(db, website, monkeypatch):
    url, website_state = website
    website_state["dynamic"] = True
    binding, task = seed(db, url)
    calls = []
    mock_planner(monkeypatch, calls)
    result = collect_official(row(binding), {"max_pages": 2, "max_details": 5}, task.id)
    assert len(result.records) == 2
    assert calls[0]["render_mode"] == "browser"
    assert result.coverage == "complete"


def test_broad_or_whole_page_selectors_are_rejected():
    with pytest.raises(ValidationError):
        WebsitePlan(job_link_selector="a[href]", title_selector="h1", description_selector=".desc")
    with pytest.raises(ValidationError):
        WebsitePlan(job_link_selector=".job", title_selector="h1", description_selector="body")


def test_navigation_is_not_a_job_and_total_is_not_current_page_number():
    soup = BeautifulSoup(
        '<nav><a href="/job/privacy">隐私政策</a></nav>'
        '<a class="job" href="/job/1">工程师</a>'
        '<span id="total">显示1-10条，共2235个职位</span>'
        '<span id="page">1</span><div>共<span id="number">2</span>个职位</div>', "html.parser"
    )
    assert eligible_links(soup, "a[href]", "https://example.com/jobs") == ["https://example.com/job/1"]
    assert declared_total(soup, "#total") == 2235
    assert declared_total(soup, "#page") is None
    assert declared_total(soup, "#number") == 2


def test_learning_does_not_overwrite_concurrent_source_edit(db, website, monkeypatch):
    from app.db import session_factory
    from app.website_agent import collect_with_plan
    url, _ = website
    binding, task = seed(db, url)
    calls = []
    mock_planner(monkeypatch, calls)

    def edit_while_collecting(*args, **kwargs):
        result = collect_with_plan(*args, **kwargs)
        with session_factory()() as changed:
            current = changed.get(SourceBinding, binding.id)
            current.entry_url = url + "?new-source=1"
            current.config = {"user_revision": "keep"}
            changed.commit()
        return result

    monkeypatch.setattr("app.website_agent.collect_with_plan", edit_while_collecting)
    result = collect_official(row(binding), {"max_pages": 2, "max_details": 5}, task.id)
    db.refresh(binding)
    assert len(result.records) == 2 and result.adapter_fingerprint
    assert binding.config == {"user_revision": "keep"}
    assert binding.entry_url.endswith("?new-source=1")
