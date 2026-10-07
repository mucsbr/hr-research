import json
import subprocess
import sys
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.bettafish_bridge import BettaFishBridge, compact_prompt_json, budgeted_prompts, static_report_html
from app.collection import CollectionError
from app.config import get_settings
from app.llm import ModelError, ModelGateway
from app.models import ModelCall, TaskRun


@pytest.fixture
def fake_upstream(tmp_path, monkeypatch, db):
    """Executable contract fixture, not a claim that live BettaFish was deployed."""
    repo = tmp_path / "upstream"
    (repo / "ReportEngine" / "utils").mkdir(parents=True)
    (repo / "ReportEngine" / "__init__.py").write_text("")
    (repo / "ReportEngine" / "utils" / "__init__.py").write_text("")
    (repo / "ReportEngine" / "core.py").write_text(
        "def parse_template_sections(template):\n    return [object()]\n")
    (repo / "ReportEngine" / "prompts.py").write_text(
        "SYSTEM_PROMPT_CHAPTER_JSON = 'Contract fixture schema'\n")
    (repo / "ReportEngine" / "utils" / "config.py").write_text(
        "class Settings:\n    def __init__(self, **kwargs):\n        self.__dict__.update(kwargs)\n")
    (repo / "ReportEngine" / "agent.py").write_text('''import os, socket
LLMClient = None
class ReportAgent:
    def __init__(self, config):
        assert "LLM_API_KEY" not in os.environ
        assert "DATABASE_URL" not in os.environ
        assert os.path.dirname(config.LOG_FILE)
        self.client = LLMClient()
    def generate_report(self, query, reports, forum_logs, custom_template, save_report):
        assert forum_logs == "" and save_report is True
        assert len(reports) == 3 and reports[0] == reports[1] == ""
        try:
            socket.create_connection(("example.com", 443))
        except RuntimeError as exc:
            assert str(exc) == "bridge_worker_network_disabled"
        else:
            raise AssertionError("unmetered network was allowed")
        content = self.client.invoke("Summarize input evidence", query)
        return {"html_content": "<html><body><h1>" + query + "</h1><p>" + content + "</p></body></html>"}
''')
    for args in (["git", "init", "-q", str(repo)],
                 ["git", "-C", str(repo), "add", "."],
                 ["git", "-C", str(repo), "-c", "user.name=Sentiment Contract Test", "-c", "user.email=test@example.invalid",
                  "commit", "-qm", "contract fixture"]):
        subprocess.run(args, check=True, capture_output=True)
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    settings = get_settings()
    for key, value in {"bettafish_repo_dir": str(repo), "bettafish_python": sys.executable,
                       "bettafish_revision": revision, "bettafish_license_confirmed": True,
                       "bettafish_timeout_seconds": 15}.items():
        monkeypatch.setattr(settings, key, value)
    return repo


def request(task_id, query):
    return {"task_id": str(task_id), "query": query, "evidence": [{"url": "https://example.com/p", "content": "星河科技反馈"}],
            "coverage": {"coverage": "sample"}, "template": "# 样本报告"}


def gateway():
    def reply(request):
        payload = json.loads(request.content)
        assert payload["max_tokens"] <= get_settings().llm_max_output_tokens
        return httpx.Response(200, json={"choices": [{"message": {"content": "已采集样本观察"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 30, "completion_tokens": 10}})
    return ModelGateway(httpx.MockTransport(reply))


def test_bridge_license_gate_does_not_start_process(fake_upstream, monkeypatch):
    monkeypatch.setattr(get_settings(), "bettafish_license_confirmed", False)
    bridge = BettaFishBridge()
    assert bridge.health()["status"] == "license_pending"
    with pytest.raises(CollectionError, match="未确认"):
        bridge.analyze(uuid.uuid4(), {}, gateway=gateway())


def test_bridge_rejects_modified_upstream(fake_upstream):
    (fake_upstream / "ReportEngine" / "agent.py").write_text("modified")
    assert BettaFishBridge().health()["status"] == "version_mismatch"


def test_bridge_executes_isolated_process_and_meters_calls(fake_upstream, db):
    tasks = [TaskRun(domain="sentiment", kind="sentiment_bettafish") for _ in range(2)]
    db.add_all(tasks)
    db.commit()
    bridge = BettaFishBridge()
    first = bridge.analyze(tasks[0].id, request(tasks[0].id, "任务一"), gateway=gateway())
    second = bridge.analyze(tasks[1].id, request(tasks[1].id, "任务二"), gateway=gateway())
    assert "任务一" in first["html"] and "任务二" not in first["html"]
    assert "任务二" in second["html"] and first["sha256"] != second["sha256"]
    assert len(first["model_call_ids"]) == len(second["model_call_ids"]) == 1
    assert len(db.scalars(select(ModelCall)).all()) == 2
    for task in tasks:
        path = get_settings().storage_dir / "bettafish" / str(task.id) / "result.html"
        assert path.is_file() and path.stat().st_mode & 0o077 == 0


def test_bridge_cannot_bypass_global_budget(fake_upstream, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_daily_request_limit", 0)
    task = TaskRun(domain="sentiment", kind="sentiment_bettafish")
    db.add(task)
    db.commit()
    with pytest.raises(ModelError) as error:
        BettaFishBridge().analyze(task.id, request(task.id, "预算已用完"), gateway=gateway())
    assert error.value.code == "bettafish_call_budget"
    assert not db.scalars(select(ModelCall)).all()
    assert not (get_settings().storage_dir / "bettafish" / str(task.id) / "result.html").exists()


def test_bridge_cancellation_stops_process_without_model_call(fake_upstream, db):
    task = TaskRun(domain="sentiment", kind="sentiment_bettafish")
    db.add(task)
    db.commit()
    def cancelled():
        raise CollectionError("cancelled", "任务已取消")
    with pytest.raises(CollectionError, match="取消"):
        BettaFishBridge().analyze(task.id, request(task.id, "取消"), gateway=gateway(), cancel_check=cancelled)
    assert not db.scalars(select(ModelCall)).all()


def test_schema_compaction_is_lossless_and_keeps_instruction_text():
    schema = {"type": "object", "description": "保留  空格\n以及换行", "properties": {"内容": {"type": "string"}}}
    source = "前置指令\n<CHAPTER JSON SCHEMA>\n" + json.dumps(schema, ensure_ascii=False, indent=2) + "\n</CHAPTER JSON SCHEMA>\n后置指令"
    compact = compact_prompt_json(source)
    assert compact.startswith("前置指令\n") and compact.endswith("\n后置指令")
    assert json.loads(compact.split("<CHAPTER JSON SCHEMA>")[1].split("</CHAPTER JSON SCHEMA>")[0]) == schema
    assert len(compact) < len(source)
    evidence = {"content": "保留原文  空格\n换行", "url": "https://example.com/a?q=1"}
    assert json.loads(compact_prompt_json(json.dumps(evidence, indent=2), whole_json=True)) == evidence
    assert compact_prompt_json("not JSON", whole_json=True) == "not JSON"


def test_upstream_preflight_rejects_input_before_any_paid_phase(fake_upstream, db):
    task = TaskRun(domain="sentiment", kind="sentiment_bettafish")
    db.add(task)
    db.commit()
    with pytest.raises(ModelError) as error:
        BettaFishBridge()._validate_requirements(task.id, {
            "minimum_model_calls": 3, "minimum_input_chars": 20000,
        }, gateway())
    assert error.value.code == "bettafish_input_budget"
    assert not db.scalars(select(ModelCall)).all()


def test_upstream_preflight_rejects_known_call_overrun(fake_upstream, db):
    task = TaskRun(domain="sentiment", kind="sentiment_bettafish")
    db.add(task)
    db.commit()
    with pytest.raises(ModelError) as error:
        BettaFishBridge()._validate_requirements(task.id, {
            "minimum_model_calls": 5, "minimum_input_chars": 14000,
        }, gateway())
    assert error.value.code == "bettafish_call_budget"
    assert not db.scalars(select(ModelCall)).all()


def test_brief_budget_replaces_long_report_target_without_dropping_evidence():
    payload = {"evidence": [{"id": "one", "text": "保留  原文"}, {"id": "two", "text": "另一条"}],
               "section": {"outline": ["客户", "产品", "经营"]}}
    system, user = budgeted_prompts("总字数约40000字，可上下浮动5%，JSON必须有效。", json.dumps(payload), 1200)
    assert "总字数约40000" not in system
    parsed = json.loads(user)
    assert parsed["evidence"] == payload["evidence"] and parsed["section"] == payload["section"]
    assert parsed["platform_constraints"]["max_output_tokens_per_call"] == 1200
    assert parsed["platform_constraints"]["target_words_per_chapter"] == 400


def test_downloaded_report_retains_content_but_cannot_execute_embedded_instructions():
    from bs4 import BeautifulSoup
    output = static_report_html('''<html><head><meta http-equiv="refresh" content="0; url=https://bad.invalid"></head>
      <body onload="steal()"><h1>原文报告</h1><p>保留证据文字</p><script>alert(1)</script>
      <a href="javascript:alert(2)" onclick="x()">危险地址</a><a href="https://example.com/post">原文</a>
      <iframe srcdoc="<script>attack()</script>"></iframe></body></html>''')
    soup = BeautifulSoup(output, "html.parser")
    assert "保留证据文字" in soup.get_text()
    assert not soup.select("script, iframe, [onload], [onclick]")
    assert len(soup.select("meta[http-equiv]")) == 1
    assert "script-src 'none'" in soup.head.meta["content"]
    assert soup.find("a", string="危险地址").get("href") is None
    assert soup.find("a", string="原文")["href"] == "https://example.com/post"
