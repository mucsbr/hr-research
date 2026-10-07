import uuid

from sqlalchemy import select

from app.assistant_api import parse_message
from app.models import AuditEvent, Company, ModelCall, MonitoringPlan, RunBatch, TaskRun
from test_analytics import collected, company


def post(client, text):
    response = client.post("/api/v1/assistant/messages", json={"message": text})
    assert response.status_code == 200
    return response.json()


def execute(client, draft):
    return client.post("/api/v1/assistant/actions/execute", json={"action_token": draft["action_token"]})


def test_disclosed_grammar_handles_natural_examples():
    assert parse_message("现在更新 星河科技 的招聘")["intent"] == "collect"
    assert parse_message("星河科技最近一个月主要招什么人？")["intent"] == "query"
    parsed = parse_message("以后每天关注 星河科技 在上海的研发岗位")
    assert parsed["intent"] == "create_plan" and parsed["filters"]["cities"] == ["上海"]


def test_unmatched_input_never_calls_a_model_or_creates_tasks(analytics_client, db):
    result = post(analytics_client, "你猜猜我需要关注谁")
    assert result["type"] == "unsupported" and result["model_calls"] == 0
    assert not db.scalars(select(TaskRun)).all() and not db.scalars(select(ModelCall)).all()


def test_add_companies_draft_does_nothing_until_explicit_execute(analytics_client, db):
    draft = post(analytics_client, "添加公司：星河科技、远山科技")
    assert draft["type"] == "action_draft" and not db.scalars(select(Company)).all()
    first = execute(analytics_client, draft)
    assert first.status_code == 200 and first.json()["created"] == 2
    again = execute(analytics_client, draft)
    assert again.json()["replayed"] is True
    assert len(db.scalars(select(Company)).all()) == 2
    assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "assistant.execution")).all()) == 1


def test_collect_reuses_batch_and_one_company_task_per_target(analytics_client, db):
    first = company(db, "星河科技")
    second = company(db, "远山科技")
    draft = post(analytics_client, "更新招聘：星河科技、远山科技；城市=上海；来源=官网")
    assert draft["model_usage"] == "conditional_website_adaptation" and draft["estimated_model_calls"] is None
    response = execute(analytics_client, draft)
    assert response.status_code == 200
    batch = db.get(RunBatch, uuid.UUID(response.json()["batch_id"]))
    tasks = db.scalars(select(TaskRun).where(TaskRun.batch_id == batch.id)).all()
    assert {task.company_id for task in tasks} == {first.id, second.id}
    assert all(task.status == "blocked_configuration" for task in tasks)
    assert all(task.config_snapshot["cities"] == ["上海"] for task in tasks)
    assert execute(analytics_client, draft).json()["replayed"] is True
    assert len(db.scalars(select(TaskRun)).all()) == 2


def test_ambiguous_company_name_cannot_start_task(analytics_client, db):
    company(db, "同名公司")
    company(db, "同名公司")
    result = post(analytics_client, "更新招聘：同名公司")
    assert result["type"] == "clarification"
    assert len(result["issues"][0]["candidates"]) == 2
    assert not db.scalars(select(TaskRun)).all()
    identifier = result["issues"][0]["candidates"][0]["id"]
    assert post(analytics_client, "更新招聘：" + identifier)["type"] == "action_draft"


def test_job_query_reads_data_and_honors_filters_without_model(analytics_client, db):
    collected(db)
    before = len(db.scalars(select(TaskRun)).all())
    result = post(analytics_client, "查询岗位：星河科技；城市=上海；关键词=Python；来源=官网")
    assert result["type"] == "query_result" and len(result["items"]) == 1
    assert result["items"][0]["title"] == "研发工程师"
    assert result["model_calls"] == 0
    assert len(db.scalars(select(TaskRun)).all()) == before
    assert not db.scalars(select(ModelCall)).all()


def test_daily_plan_reuses_platform_crud_and_preserves_scope(analytics_client, db):
    target = company(db)
    draft = post(analytics_client, "每天监测：星河科技；时间=08:45；城市=上海；关键词=研发")
    response = execute(analytics_client, draft)
    assert response.status_code == 200
    plan = db.scalar(select(MonitoringPlan))
    assert plan.company_ids == [str(target.id)] and plan.schedule == "45 8 * * *"
    assert plan.timezone == "Asia/Shanghai" and plan.enabled is True
    assert plan.filters["keywords"] == ["研发"]
    again = execute(analytics_client, draft)
    assert again.json()["replayed"] is True and len(db.scalars(select(MonitoringPlan)).all()) == 1
    pause = post(analytics_client, "暂停计划：" + str(plan.id))
    assert execute(analytics_client, pause).status_code == 200
    db.expire_all()
    assert db.get(MonitoringPlan, plan.id).enabled is False


def test_pause_company_requires_clarification_for_shared_plan(analytics_client, db):
    left, right = company(db, "星河科技"), company(db, "远山科技")
    plan = MonitoringPlan(name="共同计划", enabled=True, company_ids=[str(left.id), str(right.id)])
    db.add(plan)
    db.commit()
    result = post(analytics_client, "暂停星河科技的监测")
    assert result["type"] == "clarification" and result["plans"][0]["id"] == str(plan.id)
    assert db.get(MonitoringPlan, plan.id).enabled is True


def test_comparison_draft_uses_separate_zero_model_task(analytics_client, db):
    company(db, "星河科技")
    company(db, "远山科技")
    draft = post(analytics_client, "比较招聘：星河科技、远山科技；天数=7")
    response = execute(analytics_client, draft)
    assert response.status_code == 200
    task = db.get(TaskRun, uuid.UUID(response.json()["task_id"]))
    assert task.kind == "company_comparison" and task.company_id is None
    assert task.config_snapshot["days"] == 7
    assert not db.scalars(select(ModelCall)).all()


def test_invalid_or_tampered_draft_never_executes(analytics_client, db):
    draft = post(analytics_client, "添加公司：星河科技")
    token = draft["action_token"]
    response = analytics_client.post("/api/v1/assistant/actions/execute", json={"action_token": token[:-8] + "invalidx"})
    assert response.status_code == 422 and not db.scalars(select(Company)).all()


def test_unsupported_filter_not_silently_discarded(analytics_client, db):
    company(db, "星河科技")
    company(db, "远山科技")
    result = post(analytics_client, "比较招聘：星河科技、远山科技；城市=上海")
    assert result["type"] == "clarification" and not db.scalars(select(TaskRun)).all()
