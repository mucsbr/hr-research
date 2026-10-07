from sqlalchemy import select

from app.collection import CollectionResult, JobDocument
from app.models import Company, JobSourceRecord, SourceBinding, SourceRun, Subject, TaskRun
from app.schemas import RunInput
from app.tasks import create_batch
from app.workers import _execute_company


def test_changed_entry_does_not_reuse_old_delisting_scope(db, monkeypatch):
    subject = Subject(name="来源范围测试")
    db.add(subject)
    db.flush()
    company = Company(name=subject.name, subject_id=subject.id)
    db.add(company)
    db.flush()
    source = SourceBinding(company_id=company.id, kind="careers", entry_url="https://example.com/jobs")
    db.add(source)
    db.commit()

    def collect(binding, filters, task_id, progress, **kwargs):
        records = [JobDocument(source_job_id="job-1", title="工程师", url="https://example.com/jobs/1")]
        return CollectionResult(records=records if binding["entry_url"].endswith("/jobs") else [],
                                coverage="complete", pages=1, adapter_fingerprint="same-parser")

    monkeypatch.setattr("app.website_agent.collect_official", collect)

    def run():
        batch, _ = create_batch(db, RunInput(company_ids=[company.id]))
        task = db.scalar(select(TaskRun).where(TaskRun.batch_id == batch.id))
        _execute_company(task.id)
        db.expire_all()
        assert db.get(TaskRun, task.id).status == "succeeded"

    run()
    initial_scope = db.scalar(select(SourceRun.scope_hash))
    source.entry_url = "https://example.com/restricted-department"
    db.commit()
    run()
    run()
    record = db.scalar(select(JobSourceRecord))
    assert record.status == "observed" and record.missing_count == 0
    scopes = db.scalars(select(SourceRun.scope_hash).order_by(SourceRun.created_at)).all()
    assert scopes[0] == initial_scope and scopes[1] == scopes[2] != initial_scope
