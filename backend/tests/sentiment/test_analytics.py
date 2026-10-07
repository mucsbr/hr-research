from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.analytics_service import company_metrics, create_comparison, execute_comparison_task
from app.collection import CollectionResult, JobDocument
from app.ingest import ingest
from app.models import Company, Job, JobChange, ModelCall, Report, SourceBinding, SourceRun, Subject, TaskRun, now


def company(db, name="星河科技"):
    subject = Subject(name=name)
    db.add(subject)
    db.flush()
    result = Company(subject_id=subject.id, name=name)
    db.add(result)
    db.commit()
    return result


def collected(db, name="星河科技", coverage="complete", filters=None):
    target = company(db, name)
    binding = SourceBinding(company_id=target.id, kind="careers", entry_url="https://careers.example.com/" + str(target.id))
    task = TaskRun(company_id=target.id, status="running", config_snapshot=filters or {})
    db.add_all([binding, task])
    db.flush()
    source_run = SourceRun(task_id=task.id, binding_id=binding.id, scope_hash="same-scope")
    db.add(source_run)
    db.flush()
    result = CollectionResult(records=[
        JobDocument(source_job_id="1", title="研发工程师", url="https://careers.example.com/1",
                    locations=["上海"], function="研发", description="Python后端研发"),
        JobDocument(source_job_id="2", title="客户顾问", url="https://careers.example.com/2",
                    locations=["杭州"], function="销售", description="客户服务和咨询"),
    ], coverage=coverage)
    ingest(db, task, binding, source_run, result)
    task.status = "succeeded" if coverage == "complete" else "partial"
    db.commit()
    return target, binding, source_run


def test_unknown_company_metrics_keep_null_not_zero(db):
    target = company(db)
    result = company_metrics(db, target.id, days=7)
    assert result["observed_jobs"] is None and result["comparable_jobs"] is None
    assert all(day["changes"]["first_seen"] is None for day in result["daily_series"])
    assert result["changes_in_window"]["first_seen"] is None


def test_complete_source_metrics_count_baseline_separately(db):
    target, _, _ = collected(db)
    result = company_metrics(db, target.id, days=7)
    assert result["observed_jobs"] == result["comparable_jobs"] == 2
    assert result["functions"] == {"研发": 1, "销售": 1}
    assert result["daily_series"][-1]["changes"]["baseline_seen"] == 2
    assert result["daily_series"][-1]["changes"]["first_seen"] == 0
    assert result["daily_series"][0]["changes"]["baseline_seen"] is None


def test_partial_or_latest_failure_not_ranked_as_complete(db):
    target, binding, _ = collected(db, coverage="partial")
    sample = company_metrics(db, target.id)
    assert sample["observed_jobs"] == 2 and sample["comparable_jobs"] is None
    task = TaskRun(company_id=target.id, status="failed")
    db.add(task)
    db.flush()
    db.add(SourceRun(task_id=task.id, binding_id=binding.id, status="failed", scope_hash="same-scope"))
    db.commit()
    result = company_metrics(db, target.id)
    assert result["observed_jobs"] is None and result["historical_source_records"] == 2


def test_merged_jobs_do_not_inflate_analytics(db):
    target, _, _ = collected(db)
    job = db.scalar(select(Job).where(Job.company_id == target.id))
    job.status = "merged"
    db.commit()
    result = company_metrics(db, target.id)
    assert result["observed_jobs"] == 1
    assert result["daily_series"][-1]["changes"]["baseline_seen"] == 1


def test_daily_scope_change_is_flagged(db):
    target, binding, source_run = collected(db)
    source_run.created_at = now() - timedelta(days=1)
    db.commit()
    newer = SourceRun(task_id=source_run.task_id, binding_id=binding.id, status="succeeded", coverage="complete", scope_hash="changed", counts={"observed": 0})
    # Different task is necessary because source-run uniqueness is per task/source.
    task = TaskRun(company_id=target.id, status="succeeded")
    db.add(task)
    db.flush()
    newer.task_id = task.id
    db.add(newer)
    db.commit()
    result = company_metrics(db, target.id, days=3)
    assert result["daily_series"][-1]["scope_changed"] is True
    assert result["daily_series"][-1]["trend_comparable"] is False


def test_comparison_is_independent_zero_model_and_freezes_names(db):
    left, _, _ = collected(db, "星河科技")
    right, _, _ = collected(db, "远山科技")
    task, created = create_comparison(db, [left.id, right.id], days=7, idempotency_key="compare-one")
    assert created and task.company_id is None and task.config_snapshot["time_from"]
    right.name = "后续改名"
    db.commit()
    assert execute_comparison_task(task.id) == "succeeded"
    report = db.scalar(select(Report))
    assert "远山科技" in report.content and "后续改名" not in report.content
    assert report.coverage["comparison"][1]["difference_from_first"] == 0
    assert not db.scalars(select(ModelCall)).all()
    again, created = create_comparison(db, [left.id, right.id], days=7, idempotency_key="compare-one")
    assert again.id == task.id and not created
    with pytest.raises(HTTPException):
        create_comparison(db, [left.id, right.id], days=30, idempotency_key="compare-one")


def test_comparison_different_filters_or_missing_data_keep_differences_null(db):
    left, _, _ = collected(db, "星河科技")
    right, _, _ = collected(db, "远山科技", filters={"keywords": ["Python"]})
    missing = company(db, "还未采集")
    task, _ = create_comparison(db, [left.id, right.id, missing.id])
    assert execute_comparison_task(task.id) == "partial"
    report = db.scalar(select(Report))
    assert report.coverage["comparable"] is False
    assert all(entry["difference_from_first"] is None for entry in report.coverage["comparison"])
    assert report.coverage["comparison"][-1]["observed_jobs"] is None


def test_later_mutations_do_not_change_frozen_comparison_inputs(db):
    left, _, _ = collected(db, "星河科技")
    right, binding, _ = collected(db, "远山科技")
    task, _ = create_comparison(db, [left.id, right.id])
    change = db.scalar(select(JobChange))
    change.created_at = now() + timedelta(days=1)
    binding.enabled = False
    right.archived = True
    db.scalar(select(Job).where(Job.company_id == right.id)).status = "merged"
    db.commit()
    execute_comparison_task(task.id)
    report = db.scalar(select(Report))
    assert sum(entry["changes_in_window"]["baseline_seen"] or 0 for entry in report.coverage["companies"]) == 4
    assert all(entry["observed_jobs"] == 2 for entry in report.coverage["comparison"])


def test_analytics_api_requires_valid_window(analytics_client, db):
    target = company(db)
    assert analytics_client.get("/api/v1/recruitment/metrics", params={"company_id": str(target.id), "days": 0}).status_code == 422
    response = analytics_client.get("/api/v1/recruitment/metrics", params={"company_id": str(target.id), "days": 3})
    assert response.json()["observed_jobs"] is None
