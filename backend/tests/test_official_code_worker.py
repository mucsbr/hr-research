"""Native website failure reaches the real Worker code queue without a second page/model read."""
from sqlalchemy import func, select

from app.adapter_code_models import AdapterCodeProposal
from app.adapter_maintenance import ensure_registry
from app.auto_maintenance_models import AdapterRepairIncident
from app.models import Company, ModelCall, SourceBinding, SourceRun, Subject, TaskRun
from app.official_checkpoint import plan_digest
from app.schemas import RunInput
from app.tasks import create_batch
from app.workers import _execute_company
import test_official_program_collector as website_support


def test_native_official_worker_queues_code_from_actual_captured_failure(db, monkeypatch):
    subject = Subject(name="甲公司")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name=subject.name)
    db.add(company)
    db.flush()
    plan = {**website_support.PLAN, "description_selector": ".obsolete-description"}
    binding = SourceBinding(company_id=company.id, kind="careers", entry_url=website_support.ENTRY,
                            config={"website_plan": plan, "plan_hash": plan_digest(plan)})
    db.add(binding)
    registry = ensure_registry(db, "official")
    registry.code_agent_enabled = True
    db.commit()
    pages = website_support.Pages({
        website_support.ENTRY: '<a id="j1" class="job" href="/jobs/1">研发工程师</a>',
        website_support.ENTRY + "/1": '<h1>研发工程师</h1><p class="description">' + website_support.DESCRIPTION + '</p>',
    })
    monkeypatch.setattr("app.collection.PublicFetcher", lambda: pages.factory("http"))
    def no_rule_model(*args, **kwargs):
        raise AssertionError("已有规则解析失败应交给已启用代码维护，不先触发第二套模型")
    monkeypatch.setattr("app.website_agent.website_graph.invoke", no_rule_model)
    batch, _ = create_batch(db, RunInput(company_ids=[company.id]))
    task = db.scalar(select(TaskRun).where(TaskRun.batch_id == batch.id))
    _execute_company(task.id)
    db.expire_all()
    source = db.scalar(select(SourceRun).where(SourceRun.task_id == task.id))
    incident = db.scalar(select(AdapterRepairIncident).where(AdapterRepairIncident.task_id == task.id))
    assert source.status == "failed" and source.error_code == "selector_mismatch"
    assert incident is not None and incident.status == "code_queued", incident.details if incident else source.error_message
    assert incident.evidence["type"] == "program_input" and incident.evidence["operation"] == "parse_detail"
    proposal = db.scalar(select(AdapterCodeProposal).where(AdapterCodeProposal.task_id == incident.maintenance_task_id))
    assert proposal is not None and any(case.get("role") == "failure" for case in proposal.evidence_cases)
    assert not source.counts.get("code_execution", {}).get("verified")
    assert len(pages.calls) == 2 and db.scalar(select(func.count()).select_from(ModelCall)) == 0
