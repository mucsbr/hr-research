from sqlalchemy import select
from datetime import timedelta

from app.collection import CollectionResult, JobDocument
from app.ingest import ingest
from app.models import Company, JobChange, JobSourceRecord, SourceBinding, SourceRun, Subject, TaskRun
from app.models import JobObservation, JobVersion, now


def setup_company(db):
    subject = Subject(name="测试公司")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name="测试公司")
    db.add(company)
    db.flush()
    binding = SourceBinding(company_id=company.id, kind="careers",
                            entry_url="https://example.com/jobs")
    db.add(binding)
    db.commit()
    return company, binding


def observe(db, company, binding, records, coverage="complete", scope="same"):
    task = TaskRun(company_id=company.id, config_snapshot={}, scope_hash=scope)
    db.add(task)
    db.flush()
    run = SourceRun(task_id=task.id, binding_id=binding.id, scope_hash=scope)
    db.add(run)
    db.flush()
    counts = ingest(db, task, binding, run, CollectionResult(
        records=records, coverage=coverage, pages=1
    ))
    db.commit()
    return run, counts


def doc(**values):
    return JobDocument(**{"source_job_id": "position-1", "title": "研发工程师",
                          "url": "https://example.com/jobs/1", "description": "研发平台",
                          "locations": ["上海"], **values})


def test_checkpoint_resume_same_source_run_is_idempotent_and_preserves_seen_time(db):
    company, binding = setup_company(db)
    captured = now() - timedelta(minutes=10)
    first = doc(observed_at=captured)
    run, _ = observe(db, company, binding, [first], coverage="partial")
    task = db.get(TaskRun, run.task_id)
    result = CollectionResult(records=[first, doc(source_job_id="position-2", observed_at=captured)],
                              coverage="complete", pages=2)
    counts = ingest(db, task, binding, run, result)
    db.commit()
    assert counts["observed"] == 2 and counts["baseline_seen"] == 2
    ingest(db, task, binding, run, result)
    db.commit()
    assert len(db.scalars(select(JobObservation).where(JobObservation.source_run_id == run.id)).all()) == 2
    assert len(db.scalars(select(JobChange).where(JobChange.source_run_id == run.id)).all()) == 2
    records = db.scalars(select(JobSourceRecord)).all()
    assert all(record.first_seen_at == captured and record.last_seen_at == captured for record in records)
    assert all("observed_at" not in version.data for version in db.scalars(select(JobVersion)))


def test_resumed_old_content_cannot_replace_newer_live_observation(db):
    company, binding = setup_company(db)
    old, recent = now() - timedelta(minutes=20), now() - timedelta(minutes=1)
    observe(db, company, binding, [doc(description="最新要求", observed_at=recent)])
    run, _ = observe(db, company, binding, [doc(description="缓存的旧要求", observed_at=old)])
    record = db.scalar(select(JobSourceRecord))
    assert record.data["description"] == "最新要求" and record.last_seen_at == recent
    assert record.first_seen_at == old
    assert run.coverage == "partial" and run.counts["stale_observations"] == 1
    assert not db.scalars(select(JobChange).where(JobChange.source_run_id == run.id)).all()


def test_replaying_complete_source_run_does_not_count_missing_twice(db):
    company, binding = setup_company(db)
    observe(db, company, binding, [doc()])
    run, _ = observe(db, company, binding, [])
    assert db.scalar(select(JobSourceRecord)).missing_count == 1
    ingest(db, db.get(TaskRun, run.task_id), binding, run, CollectionResult(coverage="complete"))
    db.commit()
    assert db.scalar(select(JobSourceRecord)).missing_count == 1
    assert db.scalar(select(JobSourceRecord)).status == "observed"


def test_resumed_listing_cannot_delist_job_observed_after_its_snapshot_started(db):
    company, binding = setup_company(db)
    early, recent = now() - timedelta(minutes=20), now() - timedelta(minutes=1)
    observe(db, company, binding, [doc(observed_at=recent)])
    task = TaskRun(company_id=company.id, config_snapshot={}, scope_hash="same")
    db.add(task)
    db.flush()
    run = SourceRun(task_id=task.id, binding_id=binding.id, scope_hash="same")
    db.add(run)
    db.flush()
    result = CollectionResult(records=[], coverage="complete", pages=1,
                              checkpoint_info={"resumed": True, "collection_window_start": early.isoformat()})
    ingest(db, task, binding, run, result)
    db.commit()
    assert run.coverage == "partial" and run.counts["newer_observations_outside_checkpoint"] == 1
    assert db.scalar(select(JobSourceRecord)).missing_count == 0


def test_first_snapshot_is_baseline_not_new_publication(db):
    company, binding = setup_company(db)
    _, counts = observe(db, company, binding, [doc()])
    assert counts["baseline_seen"] == 1 and counts["first_seen"] == 0
    _, counts = observe(db, company, binding, [doc(), doc(source_job_id="position-2")])
    assert counts["first_seen"] == 1


def test_format_changes_do_not_create_content_events(db):
    company, binding = setup_company(db)
    observe(db, company, binding, [doc(description="研发 平台")])
    _, counts = observe(db, company, binding, [doc(description="研发   平台")])
    assert counts["content_changed"] == 0
    _, counts = observe(db, company, binding, [doc(description="新增模型训练职责")])
    assert counts["content_changed"] == 1


def test_partial_runs_never_mark_missing_jobs_removed(db):
    company, binding = setup_company(db)
    observe(db, company, binding, [doc()])
    for _ in range(3):
        observe(db, company, binding, [], coverage="partial")
    record = db.scalar(select(JobSourceRecord))
    assert record.status == "observed" and record.missing_count == 0


def test_partial_snapshot_can_discover_new_jobs_without_claiming_full_coverage(db):
    company, binding = setup_company(db)
    _, first = observe(db, company, binding, [doc()], coverage="partial")
    _, second = observe(db, company, binding, [
        doc(), doc(source_job_id="position-2", title="新观察到的岗位")
    ], coverage="partial")
    assert first["baseline_seen"] == 1
    assert second["first_seen"] == 1 and second["baseline_seen"] == 0
    assert second["suspected_removed"] == 0


def test_removal_requires_two_complete_observations_and_reappearance_is_recorded(db):
    company, binding = setup_company(db)
    observe(db, company, binding, [doc()])
    observe(db, company, binding, [])
    assert db.scalar(select(JobSourceRecord)).status == "observed"
    _, counts = observe(db, company, binding, [])
    assert counts["suspected_removed"] == 1
    _, counts = observe(db, company, binding, [doc()])
    assert counts["reappeared"] == 1
    assert db.scalar(select(JobSourceRecord)).status == "observed"


def test_changed_scope_does_not_infer_removal(db):
    company, binding = setup_company(db)
    observe(db, company, binding, [doc()], scope="shanghai")
    observe(db, company, binding, [], scope="beijing")
    observe(db, company, binding, [], scope="beijing")
    assert db.scalar(select(JobSourceRecord)).status == "observed"
    assert not db.scalars(select(JobChange).where(JobChange.type == "suspected_removed")).all()


def test_same_source_id_in_different_companies_is_isolated(db):
    a, source_a = setup_company(db)
    b, source_b = setup_company(db)
    observe(db, a, source_a, [doc()])
    observe(db, b, source_b, [doc(title="不同公司职位")])
    records = db.scalars(select(JobSourceRecord)).all()
    assert len(records) == 2 and records[0].job_id != records[1].job_id
