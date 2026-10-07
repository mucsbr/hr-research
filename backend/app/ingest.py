import hashlib
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.collection import CollectionResult, matches_filters
from app.config import get_settings
from app.models import (
    Company, Job, JobChange, JobObservation, JobSourceRecord, JobVersion, RawDocument,
    SourceBinding, SourceRun, TaskRun, now,
)
from app.notifications import emit_event


def ingest(db: Session, task: TaskRun, binding: SourceBinding,
           run: SourceRun, result: CollectionResult) -> dict:
    # One transaction per source: failed parsing/network cannot replace a complete baseline.
    company = db.get(Company, task.company_id)
    complete_applied = bool((run.counts or {}).get("complete_observation_applied"))
    stale_observations = 0

    def notify_change(record, event_type, title):
        if event_type == "baseline_seen":
            return
        emit_event(db, event_type="recruitment." + event_type,
                   dedupe_key=f"job:{run.id}:{record.id}:{event_type}",
                   title=f"{company.name} · {title}", text=f"岗位观察变化：{event_type}",
                   subject_id=company.subject_id, task_id=task.id, url=record.url)

    previous_complete = db.scalar(select(func.count()).select_from(SourceRun).where(
        SourceRun.binding_id == binding.id, SourceRun.id != run.id,
        SourceRun.scope_hash == run.scope_hash, SourceRun.status == "succeeded",
        SourceRun.coverage == "complete",
    ))
    previous_snapshot = db.scalar(select(func.count()).select_from(SourceRun).where(
        SourceRun.binding_id == binding.id, SourceRun.id != run.id,
        SourceRun.scope_hash == run.scope_hash,
        or_(and_(SourceRun.status == "succeeded", SourceRun.coverage == "complete"),
            SourceRun.counts["observed"].as_integer() > 0),
    ))
    raw_refs = {}
    folder = get_settings().storage_dir / "tasks" / str(task.id)
    folder.mkdir(parents=True, exist_ok=True)
    for url, content, content_type in result.documents:
        digest = hashlib.sha256(content.encode()).hexdigest()
        existing = db.scalar(select(RawDocument).where(RawDocument.task_id == task.id,
                                                       RawDocument.url == url, RawDocument.sha256 == digest))
        if existing:
            raw_refs[url] = existing.id
            continue
        target = folder / (digest + ".txt")
        target.write_text(content, encoding="utf-8")
        document = RawDocument(task_id=task.id, url=url, sha256=digest,
                               storage_ref=str(target.relative_to(get_settings().storage_dir)),
                               content_type=content_type)
        db.add(document)
        db.flush()
        raw_refs[url] = document.id
    counts = {"observed": 0, "baseline_seen": 0, "first_seen": 0,
              "content_changed": 0, "salary_changed": 0,
              "suspected_removed": 0, "reappeared": 0}
    seen_ids = set()
    for doc in result.records:
        if not matches_filters(doc, task.config_snapshot):
            continue
        record = db.scalar(select(JobSourceRecord).where(
            JobSourceRecord.binding_id == binding.id,
            JobSourceRecord.source_job_id == doc.source_job_id,
        ).with_for_update())
        if record and record.id in seen_ids:
            continue
        digest = doc.content_hash()
        data = doc.model_dump(exclude={"raw_ref", "observed_at"})
        observed_at = doc.observed_at or now()
        stale = bool(record and observed_at < record.last_seen_at)
        if stale:
            stale_observations += 1
            record.first_seen_at = min(record.first_seen_at, observed_at)
        events = []
        if record is None:
            job = Job(company_id=task.company_id, title=doc.title, locations=doc.locations,
                      function=doc.function)
            db.add(job)
            db.flush()
            record = JobSourceRecord(
                job_id=job.id, binding_id=binding.id, source_job_id=doc.source_job_id,
                url=doc.url, recruiting_entity=doc.recruiting_entity, current_hash=digest,
                data=data, baseline_scope=run.scope_hash,
                first_seen_at=observed_at, last_seen_at=observed_at,
            )
            db.add(record)
            db.flush()
            events.append(("first_seen" if previous_snapshot else "baseline_seen", {}))
        elif not stale:
            if record.status == "suspected_removed":
                events.append(("reappeared", {}))
            if record.current_hash != digest:
                diff = {key: {"before": record.data.get(key), "after": value}
                        for key, value in data.items()
                        if key not in {"source_published_at", "source_updated_at", "url"}
                        and record.data.get(key) != value}
                if "salary_raw" in diff or "currency" in diff:
                    events.append(("salary_changed", diff))
                else:
                    events.append(("content_changed", diff))
            record.current_hash, record.data = digest, data
            record.url, record.recruiting_entity = doc.url, doc.recruiting_entity
            record.last_seen_at = max(record.last_seen_at, observed_at)
            record.status, record.missing_count = "observed", 0
            record.baseline_scope = run.scope_hash
            job = db.get(Job, record.job_id)
            job.title, job.locations, job.status = doc.title, doc.locations, "observed"
            job.updated_at = now()
        seen_ids.add(record.id)
        counts["observed"] += 1
        version = db.scalar(select(JobVersion).where(
            JobVersion.record_id == record.id, JobVersion.content_hash == digest))
        if not version:
            version = JobVersion(record_id=record.id, content_hash=digest, data=data,
                                 raw_document_id=raw_refs.get(doc.raw_ref))
            db.add(version)
            db.flush()
        observation = db.scalar(select(JobObservation).where(
            JobObservation.record_id == record.id, JobObservation.source_run_id == run.id))
        if not observation:
            db.add(JobObservation(record_id=record.id, source_run_id=run.id,
                                   version_id=version.id, created_at=observed_at))
        elif not stale and observation.version_id != version.id:
            observation.version_id = version.id
        for event_type, details in events:
            exists = db.scalar(select(JobChange.id).where(JobChange.record_id == record.id,
                                JobChange.source_run_id == run.id, JobChange.type == event_type))
            if not exists:
                db.add(JobChange(record_id=record.id, source_run_id=run.id,
                                 type=event_type, details=details, created_at=observed_at))
                notify_change(record, event_type, doc.title)
    stale_observations = max(stale_observations, (run.counts or {}).get("stale_observations", 0))
    newer_unseen = 0
    if result.checkpoint_info.get("resumed") and result.checkpoint_info.get("collection_window_start"):
        window_start = datetime.fromisoformat(result.checkpoint_info["collection_window_start"])
        query = select(func.count()).select_from(JobSourceRecord).where(
            JobSourceRecord.binding_id == binding.id, JobSourceRecord.last_seen_at > window_start)
        if seen_ids:
            query = query.where(JobSourceRecord.id.not_in(seen_ids))
        newer_unseen = db.scalar(query)
    incomplete_checkpoint = bool(stale_observations or newer_unseen)
    coverage = "partial" if incomplete_checkpoint and result.coverage == "complete" else result.coverage
    if coverage == "complete" and previous_complete and not complete_applied:
        query = select(JobSourceRecord).where(
            JobSourceRecord.binding_id == binding.id,
            JobSourceRecord.baseline_scope == run.scope_hash,
            JobSourceRecord.status == "observed",
        )
        if seen_ids:
            query = query.where(JobSourceRecord.id.not_in(seen_ids))
        for record in db.scalars(query.with_for_update()):
            record.missing_count += 1
            if record.missing_count >= 2:
                record.status = "suspected_removed"
                db.add(JobChange(record_id=record.id, source_run_id=run.id,
                                 type="suspected_removed", details={"complete_misses": 2}))
                notify_change(record, "suspected_removed", db.get(Job, record.job_id).title)
                # A canonical job remains observed if another source still has it.
                other = db.scalar(select(func.count()).select_from(JobSourceRecord).where(
                    JobSourceRecord.job_id == record.job_id,
                    JobSourceRecord.id != record.id, JobSourceRecord.status == "observed"))
                if not other:
                    db.get(Job, record.job_id).status = "suspected_removed"
    db.flush()
    counts["observed"] = db.scalar(select(func.count()).select_from(JobObservation).where(JobObservation.source_run_id == run.id))
    for event_type, amount in db.execute(select(JobChange.type, func.count()).where(
        JobChange.source_run_id == run.id).group_by(JobChange.type)):
        counts[event_type] = amount
    run.coverage = coverage
    run.status = "succeeded" if coverage == "complete" else "partial"
    run.counts = {**counts, "pages": result.pages,
                  "stop_reason": "检查点包含更早观察，未覆盖较新数据或推断下架" if incomplete_checkpoint else result.stop_reason,
                  "stale_observations": stale_observations,
                  "newer_observations_outside_checkpoint": newer_unseen,
                  "complete_observation_applied": complete_applied or coverage == "complete",
                  "checkpoint": result.checkpoint_info}
    binding.health, binding.last_success_at = "ready", now()
    return counts
