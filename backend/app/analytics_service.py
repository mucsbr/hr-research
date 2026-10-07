"""Deterministic recruitment statistics; missing coverage is a gap, never a zero."""
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, text, update

from app.db import session_factory
from app.models import (
    Company, Job, JobChange, JobObservation, JobSourceRecord, JobVersion, Report, SourceBinding,
    SourceRun, TaskEvent, TaskRun, now,
)
from app.notifications import emit_event
from app.tasks import scope_digest

CHANGE_TYPES = ("baseline_seen", "first_seen", "content_changed", "salary_changed", "suspected_removed",
                "explicitly_closed", "reappeared")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def comparison_scope(snapshot):
    return {key: sorted(snapshot.get(key) or []) for key in ("cities", "keywords", "exclusions", "sources")}


def company_metrics(db, company_id, *, days=30, as_of=None):
    company = db.get(Company, company_id)
    if not company or company.archived:
        raise HTTPException(404, "公司不存在或已归档")
    as_of = as_of or now()
    days = min(max(days, 1), 365)
    first_day = as_of.astimezone(SHANGHAI).date() - timedelta(days=days - 1)
    start = as_of.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    bindings = db.scalars(select(SourceBinding).where(SourceBinding.company_id == company_id,
                                                     SourceBinding.enabled.is_(True))).all()
    binding_ids = [binding.id for binding in bindings]
    coverage, scopes, valid_ids, valid_run_ids, complete_ids = [], [], set(), [], set()
    for binding in bindings:
        latest = db.scalar(select(SourceRun).where(SourceRun.binding_id == binding.id,
                                                   SourceRun.updated_at <= as_of)
                           .order_by(SourceRun.created_at.desc()).limit(1))
        latest_good = db.scalar(select(SourceRun).where(SourceRun.binding_id == binding.id,
                         SourceRun.status.in_(["succeeded", "partial"]), SourceRun.updated_at <= as_of)
                               .order_by(SourceRun.created_at.desc()).limit(1))
        is_valid = bool(latest and latest.status in {"succeeded", "partial"}
                        and (latest.coverage == "complete" or latest.counts.get("observed", 0) > 0))
        if is_valid:
            valid_ids.add(binding.id)
            valid_run_ids.append(latest.id)
            task = db.get(TaskRun, latest.task_id)
            scopes.append(comparison_scope(task.config_snapshot))
            if latest.status == "succeeded" and latest.coverage == "complete":
                complete_ids.add(binding.id)
        coverage.append({"source_id": str(binding.id), "kind": binding.kind, "url": binding.entry_url,
                         "status": latest.status if latest else "not_collected",
                         "coverage": latest.coverage if latest else "unknown",
                         "scope_hash": latest.scope_hash if latest else None,
                         "last_attempt_at": latest.created_at.isoformat() if latest else None,
                         "last_success_at": latest_good.created_at.isoformat() if latest_good else None,
                         "error_code": latest.error_code if latest else None})
    records = db.execute(select(JobSourceRecord, Job).join(Job, JobSourceRecord.job_id == Job.id).where(
        Job.company_id == company_id, Job.status != "merged", JobSourceRecord.binding_id.in_(binding_ids),
        JobSourceRecord.first_seen_at <= as_of)).all()
    observed = db.execute(select(JobSourceRecord, Job, JobVersion).join(
        Job, JobSourceRecord.job_id == Job.id).join(JobObservation, JobObservation.record_id == JobSourceRecord.id)
        .join(JobVersion, JobObservation.version_id == JobVersion.id).where(
            JobObservation.source_run_id.in_(valid_run_ids), Job.status != "merged")).all()
    jobs = {job.id: version.data for _, job, version in observed}
    has_data = bool(valid_ids)
    complete = bool(bindings) and len(complete_ids) == len(bindings)
    scope_keys = {scope_digest(scope) for scope in scopes}
    limits = ["公开岗位数不是实际招聘人数；首次发现不等于当天发布。",
              "来源记录数与已关联去重岗位数分别统计；未确认的跨来源重复仍保留。"]
    if not bindings:
        limits.append("该公司未配置启用的招聘来源。")
    elif not has_data:
        limits.append("最近来源未成功提供可用覆盖，岗位统计未知；历史数据没有被解释为当前零岗位。")
    elif not complete:
        limits.append("存在部分覆盖或失败来源，观察数量只是样本，不提供可比较的全范围岗位数。")
    if len(scope_keys) > 1:
        limits.append("当前不同来源的城市或关键词筛选范围不同，不能直接汇总为一致范围。")
    functions = Counter(data.get("function") or "未分类" for data in jobs.values())
    cities = Counter(city for data in jobs.values() for city in (data.get("locations") or ["未标注"]))
    changes = db.execute(select(JobChange, SourceRun, Job).join(
        SourceRun, JobChange.source_run_id == SourceRun.id).join(
        JobSourceRecord, JobChange.record_id == JobSourceRecord.id).join(
        Job, JobSourceRecord.job_id == Job.id).where(
        Job.company_id == company_id, Job.status != "merged", SourceRun.binding_id.in_(binding_ids),
        JobChange.created_at >= start, JobChange.created_at <= as_of,
        SourceRun.status.in_(["succeeded", "partial"]))).all()
    run_rows = db.scalars(select(SourceRun).where(SourceRun.binding_id.in_(binding_ids),
                         SourceRun.created_at >= start, SourceRun.updated_at <= as_of)
                          .order_by(SourceRun.created_at)).all()
    day_runs = defaultdict(list)
    day_changes = defaultdict(Counter)
    for run in run_rows:
        day_runs[run.created_at.astimezone(SHANGHAI).date()].append(run)
    for change, run, _ in changes:
        day_changes[change.created_at.astimezone(SHANGHAI).date()][change.type] += 1
    # Temporal comparability is assessed per source, because adapter scope hashes differ by source.
    previous_scope = {}
    for binding in bindings:
        previous = db.scalar(select(SourceRun).where(SourceRun.binding_id == binding.id,
                             SourceRun.created_at < start, SourceRun.status.in_(["succeeded", "partial"]))
                             .order_by(SourceRun.created_at.desc()).limit(1))
        if previous:
            previous_scope[binding.id] = previous.scope_hash
    series = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        runs = day_runs[day]
        latest_by_source = {run.binding_id: run for run in runs}
        successful = [run for run in runs if run.status in {"succeeded", "partial"}]
        scope_changed = False
        for run in successful:
            if run.binding_id in previous_scope and previous_scope[run.binding_id] != run.scope_hash:
                scope_changed = True
            previous_scope[run.binding_id] = run.scope_hash
        full_day = bool(bindings) and len(latest_by_source) == len(bindings) and all(
            run.status == "succeeded" and run.coverage == "complete" for run in latest_by_source.values())
        known = bool(successful)
        counts = {key: day_changes[day][key] if day_changes[day][key] else 0 if full_day else None
                  for key in CHANGE_TYPES}
        series.append({"date": day.isoformat(), "coverage": "complete" if full_day else "partial" if known else "unknown",
                       "collected_sources": len(latest_by_source), "changes": counts,
                       "scope_changed": scope_changed, "trend_comparable": full_day and not scope_changed,
                       "note": "未采集日期保留空值；事件数按来源记录统计，合并岗位不重复出现在岗位总量。"})
    comparable_jobs = len(jobs) if complete and len(scope_keys) <= 1 else None
    evidence = [{"job_id": str(job.id), "record_id": str(record.id), "title": job.title,
                 "url": version.data.get("url") or record.url, "locations": version.data.get("locations", []),
                 "function": version.data.get("function"),
                 "last_seen_at": record.last_seen_at.isoformat()} for record, job, version in observed[:6]]
    event_counts = Counter(change.type for change, _, _ in changes)
    complete_window = all(point["trend_comparable"] for point in series)
    return {"company_id": str(company.id), "company_name": company.name,
            "as_of": as_of.isoformat(), "days": days, "timezone": "Asia/Shanghai",
            "data_status": "complete" if comparable_jobs is not None else "partial" if has_data else "unknown",
            "observed_jobs": len(jobs) if has_data else None,
            "observed_source_records": len(observed) if has_data else None,
            "historical_source_records": len(records), "comparable_jobs": comparable_jobs,
            "functions": dict(functions) if has_data else None, "cities": dict(cities) if has_data else None,
            "source_coverage": coverage, "filter_scopes": scopes,
            "source_kinds": sorted({binding.kind for binding in bindings}),
            "changes_in_window": {key: event_counts[key] if event_counts[key] else 0 if complete_window else None
                                  for key in CHANGE_TYPES},
            "event_counts_are_observed_samples": True,
            "daily_series": series, "evidence": evidence, "limitations": limits}


def create_comparison(db, company_ids, *, days=30, idempotency_key=None):
    if not 1 <= days <= 365:
        raise HTTPException(422, "统计窗口应为1至365天")
    identifiers = list(dict.fromkeys(company_ids))
    if len(identifiers) < 2 or len(identifiers) > 20:
        raise HTTPException(422, "比较需要2至20家不同公司")
    companies = db.scalars(select(Company).where(Company.id.in_(identifiers), ~Company.archived)).all()
    if len(companies) != len(identifiers):
        raise HTTPException(422, "部分比较公司不存在或已归档")
    digest = scope_digest({"company_ids": sorted(str(i) for i in identifiers), "days": days})
    if idempotency_key:
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "comparison:" + idempotency_key})
        existing = db.scalar(select(TaskRun).where(TaskRun.kind == "company_comparison",
                            TaskRun.config_snapshot["idempotency_key"].astext == idempotency_key))
        if existing:
            if existing.scope_hash != digest:
                raise HTTPException(409, "同一幂等键不能用于不同的公司比较范围")
            db.commit()
            return existing, False
    snapshot_at = now()
    window_start = snapshot_at.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    # Persist bounded structured inputs at creation: later source edits, archival or
    # canonical merges must not reinterpret this report's frozen observation scope.
    metrics_snapshot = [company_metrics(db, identifier, days=days, as_of=snapshot_at) for identifier in identifiers]
    task = TaskRun(domain="recruitment", kind="company_comparison", scope_hash=digest,
                   config_snapshot={"company_ids": [str(identifier) for identifier in identifiers],
                                    "days": days, "idempotency_key": idempotency_key,
                                    "company_names": {str(company.id): company.name for company in companies},
                                    "as_of": snapshot_at.isoformat(), "time_from": window_start.isoformat(),
                                    "time_to": snapshot_at.isoformat(),
                                    "metrics_snapshot": metrics_snapshot,
                                    "engine": "deterministic_statistics"})
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, message="已创建独立跨公司比较；使用结构化统计，不调用模型"))
    db.commit()
    return task, True


def execute_comparison_task(task_id):
    identifier = uuid.UUID(str(task_id))
    with session_factory()() as db:
        claim = db.execute(update(TaskRun).where(TaskRun.id == identifier, TaskRun.kind == "company_comparison",
                            TaskRun.status == "queued", TaskRun.cancel_requested.is_(False)).values(
                            status="running", started_at=now(), heartbeat_at=now(), attempt=TaskRun.attempt + 1)
                            .returning(TaskRun.id)).first()
        db.commit()
        if not claim:
            return
        task = db.get(TaskRun, identifier)
        try:
            metrics = []
            as_of = datetime.fromisoformat(task.config_snapshot["as_of"])
            frozen_metrics = task.config_snapshot.get("metrics_snapshot")
            if not frozen_metrics or len(frozen_metrics) != len(task.config_snapshot["company_ids"]):
                raise HTTPException(422, "旧比较任务缺少结构化统计快照，请重新创建比较")
            for index, company_id in enumerate(task.config_snapshot["company_ids"]):
                db.refresh(task)
                if task.cancel_requested:
                    task.status = "cancelled"
                    break
                entry = next((entry for entry in frozen_metrics if entry["company_id"] == company_id), None)
                if entry is None:
                    raise HTTPException(422, "公司统计快照不完整，请重新创建比较")
                entry["company_name"] = task.config_snapshot["company_names"][company_id]
                metrics.append(entry)
                task.progress, task.heartbeat_at = int((index + 1) / len(task.config_snapshot["company_ids"]) * 90), now()
                db.commit()
            else:
                signatures = {scope_digest({"sources": entry["source_kinds"],
                              "filters": entry["filter_scopes"][0] if entry["filter_scopes"] else None}) for entry in metrics}
                comparable = all(entry["comparable_jobs"] is not None for entry in metrics) and len(signatures) == 1
                baseline = metrics[0]["comparable_jobs"] if comparable else None
                comparison = [{"company_id": entry["company_id"], "company_name": entry["company_name"],
                               "observed_jobs": entry["observed_jobs"],
                               "comparable_jobs": entry["comparable_jobs"] if comparable else None,
                               "difference_from_first": entry["comparable_jobs"] - baseline if comparable else None}
                              for entry in metrics]
                lines = ["招聘来源观察比较", "", f"统计窗口：最近{task.config_snapshot['days']}天（Asia/Shanghai）", ""]
                for entry in metrics:
                    value = str(entry["observed_jobs"]) if entry["observed_jobs"] is not None else "未知"
                    lines.append(f"- {entry['company_name']}：已观察岗位 {value}；来源状态 {entry['data_status']}。")
                lines += ["", "公司间数量可直接比较。" if comparable else "来源覆盖或筛选范围不同，直接数量差和排名保持空值。",
                          "首次基线与首次发现分别计算；公开岗位数不等于招聘人数。"]
                report = Report(task_id=task.id, domain="recruitment", title="竞品招聘比较",
                                content="\n".join(lines), evidence=[e for entry in metrics for e in entry["evidence"]],
                                coverage={"engine": "deterministic_statistics", "model_calls": 0, "as_of": as_of.isoformat(),
                                          "comparison_basis": "latest_complete_observed_scope_not_net_hiring_change",
                                          "days": task.config_snapshot["days"], "comparable": comparable,
                                          "comparison": comparison, "companies": metrics})
                db.add(report)
                db.flush()
                emit_event(db, event_type="report.ready", dedupe_key="report:" + str(report.id),
                           title=report.title, text=report.content[:1000], task_id=task.id)
                task.summary = {"report_id": str(report.id), "company_count": len(metrics),
                                "comparable": comparable, "model_calls": 0}
                task.status = "succeeded" if comparable else "partial"
        except HTTPException as exc:
            task.status, task.error_code, task.error_message = "failed", "comparison_scope_invalid", str(exc.detail)
        except Exception:
            db.rollback()
            task = db.get(TaskRun, identifier)
            task.status, task.error_code, task.error_message = "failed", "comparison_failed", "招聘比较未完成，未调用模型"
        task.finished_at, task.progress = now(), 100
        db.commit()
        return task.status
