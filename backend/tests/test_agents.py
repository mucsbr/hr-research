import json
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.agents import company_analysis_graph
from app.collection import CollectionResult, JobDocument
from app.ingest import ingest
from app.llm import ModelError, ModelGateway
from app.models import Company, ModelCall, Report, SourceBinding, SourceRun, Subject, TaskRun
from app.tasks import create_analysis


def seed(db, name):
    subject = Subject(name=name)
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name=name)
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="careers", entry_url="https://example.com/jobs")
    db.add(binding)
    task = TaskRun(company_id=company.id, config_snapshot={}, scope_hash="test")
    db.add(task)
    db.flush()
    source_run = SourceRun(task_id=task.id, binding_id=binding.id, scope_hash="test")
    db.add(source_run)
    db.flush()
    ingest(db, task, binding, source_run, CollectionResult(
        records=[JobDocument(source_job_id="job-1", title=name + "研发岗位",
                             url="https://example.com/jobs/1", description="Python 平台开发")],
        coverage="partial",
    ))
    db.commit()
    return company


def test_analysis_reads_only_selected_company_and_validates_evidence(db, monkeypatch):
    a, b = seed(db, "甲公司"), seed(db, "乙公司")
    task, _ = create_analysis(db, a.id)
    seen = []
    def handle(request):
        payload = json.loads(request.content)
        evidence = json.loads(payload["messages"][-1]["content"])
        seen.append(evidence)
        assert evidence["company"] == "甲公司"
        assert "乙公司" not in request.content.decode()
        content = {"summary": "已观察到研发岗位", "observations": [
            {"text": "岗位涉及 Python", "job_ids": [evidence["evidence"][0]["job_id"]]}
        ], "limitations": ["仅覆盖采集样本"]}
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(content, ensure_ascii=False)}, "finish_reason": "stop"}
        ], "usage": {"prompt_tokens": 100, "completion_tokens": 40}})
    monkeypatch.setattr("app.agents.ModelGateway", lambda: ModelGateway(httpx.MockTransport(handle)))
    result = company_analysis_graph.invoke({"task_id": str(task.id), "company_id": str(a.id)})
    assert result["report_id"] and len(seen) == 1
    report = db.get(Report, uuid.UUID(result["report_id"]))
    assert report.title.startswith("甲公司")
    assert len(db.scalars(select(ModelCall)).all()) == 1
    assert b.id != a.id


def test_analysis_rejects_foreign_job_citation(db, monkeypatch):
    a = seed(db, "甲公司")
    task, _ = create_analysis(db, a.id)
    def handle(request):
        content = {"summary": "观察", "observations": [
            {"text": "错误引用", "job_ids": [str(uuid.uuid4())]}
        ], "limitations": ["样本限制"]}
        return httpx.Response(200, json={"choices": [
            {"message": {"content": json.dumps(content)}, "finish_reason": "stop"}
        ]})
    monkeypatch.setattr("app.agents.ModelGateway", lambda: ModelGateway(httpx.MockTransport(handle)))
    with pytest.raises(ModelError, match="范围之外"):
        company_analysis_graph.invoke({"task_id": str(task.id), "company_id": str(a.id)})
    assert not db.scalars(select(Report)).all()


def test_duplicate_analysis_request_reuses_active_task(db):
    a = seed(db, "甲公司")
    first, created = create_analysis(db, a.id)
    second, created_again = create_analysis(db, a.id)
    assert created and not created_again and first.id == second.id
