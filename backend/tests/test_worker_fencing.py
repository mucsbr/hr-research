import pytest
from sqlalchemy import select

from app.collection import CollectionError, CollectionResult, JobDocument
from app.config import get_settings
from app.db import session_factory
from app.execution import active_execution
from app.models import Company, Job, SourceBinding, SourceRun, Subject, TaskRun
from app.schemas import RunInput
from app.serialization import row
from app.tasks import create_batch
from app.workers import _execute_company


def seed(db):
    subject = Subject(name="代次隔离样例")
    db.add(subject)
    db.flush()
    company = Company(name=subject.name, subject_id=subject.id)
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="careers", entry_url="https://example.com/jobs")
    db.add(binding)
    db.commit()
    return company, binding


@pytest.mark.parametrize("late_failure", [False, True])
def test_old_worker_cannot_commit_success_failure_or_final_state(db, monkeypatch, late_failure):
    company, binding = seed(db)
    batch, _ = create_batch(db, RunInput(company_ids=[company.id]))
    task_id = db.scalar(select(TaskRun.id).where(TaskRun.batch_id == batch.id))

    def delayed_source(*args, **kwargs):
        with session_factory()() as newer:
            task = newer.get(TaskRun, task_id)
            task.attempt += 1
            task.summary = {"owner": "new-attempt"}
            source = newer.scalar(select(SourceRun).where(SourceRun.task_id == task_id))
            source.error_message = "new-owner-marker"
            newer.commit()
        if late_failure:
            raise CollectionError("selector_mismatch", "旧执行的错误")
        return CollectionResult(records=[JobDocument(source_job_id="old", title="旧执行结果",
                                                      url="https://example.com/jobs/old")], coverage="complete")

    monkeypatch.setattr("app.website_agent.collect_official", delayed_source)
    _execute_company(task_id)
    db.expire_all()
    task = db.get(TaskRun, task_id)
    assert task.attempt == 2 and task.status == "running" and task.finished_at is None
    assert task.summary == {"owner": "new-attempt"}
    assert db.scalar(select(SourceRun)).error_message == "new-owner-marker"
    assert not db.scalars(select(Job)).all()


def test_internal_browser_rejects_old_attempt_before_opening_any_page(signed_client, db, monkeypatch):
    company, binding = seed(db)
    binding.kind, binding.entry_url = "zhaopin", "https://www.zhaopin.com/companydetail/test"
    task = TaskRun(company_id=company.id, status="running", attempt=2,
                   config_snapshot={"company_name": company.name, "sources_snapshot": [row(binding)]})
    db.add(task)
    db.commit()
    touched = []
    monkeypatch.setattr("app.browser_internal.collect_platform", lambda *args: touched.append(True))
    response = signed_client.post("/api/internal/browser/collect", json={
        "task_id": str(task.id), "binding_id": str(binding.id), "expected_attempt": 1, "filters": {},
    }, headers={"X-HR-Worker-Token": get_settings().app_secret.get_secret_value()})
    assert response.status_code == 409 and response.json()["error_code"] == "stale_execution"
    assert touched == []


def test_generation_guard_refreshes_cached_identity_before_deciding(db):
    company, _ = seed(db)
    task = TaskRun(company_id=company.id, status="running", attempt=1)
    db.add(task)
    db.commit()
    with session_factory()() as newer:
        newer.get(TaskRun, task.id).attempt = 2
        newer.commit()
    assert task.attempt == 1  # The caller's session still holds its original object.
    with pytest.raises(CollectionError) as error:
        active_execution(db, task.id, 1)
    assert error.value.code == "stale_execution"
