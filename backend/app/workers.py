import uuid
from datetime import timedelta

import redis
import httpx
from sqlalchemy import and_, or_, select, update

from app.collection import CollectionError, CollectionResult, JobDocument, collect_tencent
from app.config import get_settings
from app.db import session_factory
from app.execution import active_execution
from app.ingest import ingest
from app.models import (
    Company, CompanyRun, MonitoringPlan, SourceAccount, SourceBinding, SourceRun, TaskEvent, TaskRun, now,
)
from app.queue import celery
from app.schemas import RunInput
from app.tasks import create_batch, next_schedule, scope_digest


def redis_client():
    return redis.Redis.from_url(get_settings().redis_url.get_secret_value())


@celery.task(name="app.dispatch_pending")
def dispatch_pending():
    with session_factory()() as db:
        # Only recover after both a stale heartbeat and the company lease have expired.
        stale = db.scalars(select(TaskRun).where(
            TaskRun.status == "running",
            TaskRun.heartbeat_at < now() - timedelta(seconds=get_settings().task_timeout_seconds + 120),
        ).with_for_update(skip_locked=True).limit(100)).all()
        client = redis_client()
        for task in stale:
            if not client.exists("company:" + str(task.company_id)):
                if task.kind != "company_collection" and task.domain != "news":
                    task.status = "failed"
                    task.error_code = "analysis_interrupted"
                    task.error_message = "任务进程中断，保留已有证据；未自动重试可能涉及模型的步骤"
                    task.finished_at = now()
                    continue
                task.status = "queued"
                db.add(TaskEvent(task_id=task.id, level="warning",
                                 message="工作进程中断，等待从已提交来源恢复"))
        db.commit()
        claimed_accounts = set()
        waiting = db.scalars(select(TaskRun).where(
            TaskRun.status.in_(["waiting_account", "waiting_login", "waiting_verification", "partial"]),
            TaskRun.cancel_requested.is_(False), TaskRun.kind == "company_collection",
        ).order_by(TaskRun.created_at).with_for_update(skip_locked=True).limit(50)).all()
        for task in waiting:
            pending_sources = db.scalars(select(SourceRun).where(
                SourceRun.task_id == task.id,
                SourceRun.status.in_(["waiting_account", "waiting_login", "waiting_verification"]),
            )).all()
            for source in pending_sources:
                binding = db.get(SourceBinding, source.binding_id)
                frozen = next((item for item in task.config_snapshot.get("sources_snapshot", [])
                               if item["id"] == str(source.binding_id)), None)
                if not binding or not frozen or frozen.get("account_id") != (
                    str(binding.account_id) if binding.account_id else None
                ):
                    source.status = "blocked_configuration"
                    source.error_message = "来源配置已改变，请重试创建新任务"
                    if task.status != "partial":
                        task.status = "blocked_configuration"
                    continue
                account = db.get(SourceAccount, binding.account_id) if binding and binding.account_id else None
                if account and account.health in {"saved", "ready"} and account.id not in claimed_accounts and not client.exists("account:" + str(account.id)):
                    task.status, task.finished_at = "queued", None
                    claimed_accounts.add(account.id)
                    break
        db.commit()
        pending = db.execute(select(TaskRun.id, TaskRun.kind).where(
            TaskRun.status == "queued", TaskRun.cancel_requested.is_(False),
            TaskRun.domain == "recruitment",
        ).order_by(TaskRun.created_at).limit(30)).all()
        sentiment_pending = db.scalars(select(TaskRun.id).where(
            TaskRun.status == "queued", TaskRun.cancel_requested.is_(False),
            TaskRun.domain == "sentiment",
        ).order_by(TaskRun.created_at).limit(20)).all()
        news_pending = db.scalars(select(TaskRun.id).where(
            TaskRun.status == "queued", TaskRun.cancel_requested.is_(False),
            TaskRun.domain == "news",
        ).order_by(TaskRun.created_at).limit(20)).all()
    for identifier, kind in pending:
        if kind == "company_analysis":
            analyze_company.apply_async(args=[str(identifier)], queue="analysis")
        elif kind == "company_comparison":
            compare_companies.apply_async(args=[str(identifier)], queue="analysis")
        else:
            collect_company.apply_async(args=[str(identifier)], queue="recruitment")
    for identifier in sentiment_pending:
        run_sentiment.apply_async(args=[str(identifier)], queue="sentiment")
    for identifier in news_pending:
        run_news.apply_async(args=[str(identifier)], queue="news")


@celery.task(name="app.run_schedules")
def run_schedules():
    with session_factory()() as db:
        ids = db.scalars(select(MonitoringPlan.id).where(
            MonitoringPlan.enabled.is_(True), MonitoringPlan.next_run_at <= now()
        ).limit(20)).all()
    for identifier in ids:
        with session_factory()() as db:
            plan = db.scalar(select(MonitoringPlan).where(
                MonitoringPlan.id == identifier, MonitoringPlan.enabled.is_(True),
                MonitoringPlan.next_run_at <= now(),
            ).with_for_update(skip_locked=True))
            if not plan:
                continue
            scheduled = plan.next_run_at
            plan.next_run_at = next_schedule(plan.schedule, plan.timezone, now())
            create_batch(db, RunInput(
                name=plan.name, company_ids=plan.company_ids, filters=plan.filters,
                idempotency_key=f"schedule:{plan.id}:{scheduled.isoformat()}",
            ), trigger="schedule", plan=plan)


def save_event(task_id, message, level="info", *, expected_attempt=None):
    with session_factory()() as db:
        task = active_execution(db, task_id, expected_attempt) if expected_attempt is not None else db.get(TaskRun, task_id)
        if not task or task.cancel_requested:
            raise CollectionError("cancelled", "任务已取消")
        if task.started_at and (now() - task.started_at).total_seconds() > get_settings().task_timeout_seconds:
            raise CollectionError("time_budget", "本次任务已达到运行时间预算")
        task.heartbeat_at = now()
        db.add(TaskEvent(task_id=task_id, message=message, level=level))
        db.commit()


@celery.task(name="app.collect_company", soft_time_limit=700, time_limit=750)
def collect_company(task_id: str):
    identifier = uuid.UUID(task_id)
    with session_factory()() as db:
        task = db.get(TaskRun, identifier)
        if not task or task.status != "queued" or task.cancel_requested:
            return
        company_id = task.company_id
    lease = redis_client().lock(
        "company:" + str(company_id), timeout=800, blocking=False, thread_local=False
    )
    if not lease.acquire():
        return
    try:
        _execute_company(identifier)
    finally:
        try:
            lease.release()
        except redis.exceptions.LockError:
            pass


def _execute_company(identifier):
    with session_factory()() as db:
        expected_attempt = db.execute(update(TaskRun).where(
            TaskRun.id == identifier, TaskRun.status == "queued",
            TaskRun.cancel_requested.is_(False),
        ).values(status="running", started_at=now(), heartbeat_at=now(),
                 attempt=TaskRun.attempt + 1).returning(TaskRun.attempt)).scalar()
        if expected_attempt is None:
            return
        db.commit()
        task = db.get(TaskRun, identifier)
        sources = task.config_snapshot.get("sources_snapshot", [])
        filters, scope_hash = task.config_snapshot, task.scope_hash
    try:
        for index, snapshot in enumerate(sources):
            save_event(identifier, f"开始采集 {snapshot['kind']} 来源", expected_attempt=expected_attempt)
            binding_id = uuid.UUID(snapshot["id"])
            source_scope = scope_digest({"filters": scope_hash, "entry_url": snapshot["entry_url"],
                                         "company_id": str(task.company_id),
                                         "external_company_id": snapshot.get("external_company_id"),
                                         "adapter": snapshot.get("adapter", "generic")})
            with session_factory()() as db:
                active_execution(db, identifier, expected_attempt)
                source_run = db.scalar(select(SourceRun).where(
                    SourceRun.task_id == identifier, SourceRun.binding_id == binding_id))
                if source_run and source_run.status in {"succeeded", "partial"}:
                    from app.checkpoint_models import OfficialCheckpoint
                    pending_checkpoint = db.scalar(select(OfficialCheckpoint).where(
                        OfficialCheckpoint.task_id == identifier, OfficialCheckpoint.binding_id == binding_id))
                    if source_run.status == "succeeded" or snapshot["kind"] != "careers" or not pending_checkpoint or pending_checkpoint.status == "complete":
                        continue
                if not source_run:
                    source_run = SourceRun(task_id=identifier, binding_id=binding_id,
                                           scope_hash=source_scope)
                    db.add(source_run)
                source_run.status = "running"
                source_run.error_code, source_run.error_message = None, None
                db.commit()
                source_run_id = source_run.id
            try:
                checkpoint = None
                if snapshot["kind"] == "careers":
                    from app.official_checkpoint import OfficialCheckpointStore
                    checkpoint = OfficialCheckpointStore.open(
                        identifier, snapshot, filters, source_run_id, expected_attempt,
                        resume_from_task_id=filters.get("resume_from_task_id"))
                if filters.get("repair_recovery"):
                    with session_factory()() as db:
                        if filters.get("code_recovery"):
                            from app.adapter_code_orchestration import prepare_code_recovery
                            prepare_code_recovery(db, identifier)
                        else:
                            from app.auto_maintenance import prepare_recovery
                            prepare_recovery(db, identifier)
                        db.commit()
                if snapshot["kind"] != "careers":
                    with session_factory()() as db:
                        aid = snapshot.get("account_id")
                        account = db.get(SourceAccount, uuid.UUID(aid)) if aid else None
                        if (account and account.health not in {"saved", "ready"}) or (
                            not account and snapshot["kind"] not in {"zhaopin", "51job"}
                        ):
                            raise CollectionError("waiting_login", "请在数据源中心完成该平台登录")
                    from app.schemas import ScopeFilters
                    native_filters = ScopeFilters.model_validate({
                        key: value for key, value in filters.items() if key in ScopeFilters.model_fields
                    })
                    settings = get_settings()
                    with httpx.Client(timeout=settings.task_timeout_seconds + 45, trust_env=False) as client:
                        response = client.post(
                            settings.browser_service_url.rstrip("/") + "/api/internal/browser/collect",
                            headers={"X-HR-Worker-Token": settings.app_secret.get_secret_value()},
                            json={"task_id": str(identifier), "binding_id": snapshot["id"],
                                  "expected_attempt": expected_attempt,
                                  "filters": native_filters.model_dump()},
                        )
                    payload = response.json()
                    if response.status_code != 200:
                        error = CollectionError(payload.get("error_code", "browser_service_error"),
                                                payload.get("detail", "浏览器服务未完成采集"))
                        error.repair_evidence = payload.get("repair_evidence")
                        error.code_failure_evidence = payload.get("code_failure_evidence")
                        error.code_execution = payload.get("code_execution", {})
                        error.adapter_fingerprint = payload.get("adapter_fingerprint", "")
                        raise error
                    result = CollectionResult(
                        records=[JobDocument.model_validate(item) for item in payload["records"]],
                        documents=[tuple(item) for item in payload["documents"]],
                        coverage=payload["coverage"], pages=payload["pages"],
                        stop_reason=payload["stop_reason"], errors=payload["errors"],
                        adapter_fingerprint=payload.get("adapter_fingerprint", ""),
                        code_execution=payload.get("code_execution", {}),
                    )
                else:
                    from app.adapter_code_runtime import freeze_for_task
                    descriptor = freeze_for_task("official", task_id=identifier, binding_id=binding_id,
                                                 expected_attempt=expected_attempt)
                    def progress(message):
                        save_event(identifier, message, expected_attempt=expected_attempt)
                    if descriptor.program:
                        if snapshot.get("adapter") == "tencent":
                            from app.tencent_program_collector import collect_tencent_program as program_collector
                        else:
                            from app.official_program_collector import collect_official_program as program_collector
                        result = program_collector(snapshot, task.config_snapshot.get("company_identity") or {
                            "name": task.config_snapshot.get("company_name"), "aliases": []}, filters,
                            identifier, source_run_id, expected_attempt, descriptor, checkpoint, progress=progress)
                    else:
                        try:
                            if snapshot.get("adapter") == "tencent":
                                result = collect_tencent(snapshot, filters, progress, checkpoint=checkpoint)
                            else:
                                from app.website_agent import collect_official
                                from app.maintenance_models import AdapterRegistry
                                with session_factory()() as db:
                                    code_repair = db.scalar(select(AdapterRegistry.code_agent_enabled).where(
                                        AdapterRegistry.kind == "official")) is True
                                options = {"defer_parse_repair": True} if code_repair else {}
                                result = collect_official(snapshot, filters, identifier, progress,
                                                          checkpoint=checkpoint, **options)
                        except CollectionError as exc:
                            from app.official_program_collector import capture_native_official_failure
                            exc.code_failure_evidence = capture_native_official_failure(snapshot,
                                task.config_snapshot.get("company_identity") or {
                                    "name": task.config_snapshot.get("company_name"), "aliases": []}, checkpoint, exc.code)
                            # Native site-plan hashes are not registry release fingerprints.
                            exc.adapter_fingerprint = descriptor.fingerprint
                            raise
                with session_factory()() as db:
                    task = active_execution(db, identifier, expected_attempt)
                    if task.cancel_requested:
                        raise CollectionError("cancelled", "任务已取消，未提交当前来源为完整基线")
                    source_run = db.get(SourceRun, source_run_id)
                    binding = db.scalar(select(SourceBinding).where(SourceBinding.id == binding_id).with_for_update()
                                        .execution_options(populate_existing=True))
                    if checkpoint:
                        checkpoint.guard_in_session(db)
                    if result.adapter_fingerprint:
                        plan_hash = result.adapter_fingerprint
                        source_run.adapter_version = snapshot["kind"] + ":" + plan_hash[:12]
                        source_run.scope_hash = scope_digest({"source": source_scope, "plan": plan_hash})
                    else:
                        source_run.scope_hash = source_scope
                    if result.code_execution and result.checkpoint_info.get("site_plan_hash"):
                        source_run.scope_hash = scope_digest({"source": source_run.scope_hash,
                            "site_plan": result.checkpoint_info["site_plan_hash"]})
                    ingest(db, task, binding, source_run, result)
                    if result.code_execution:
                        source_run.counts = {**source_run.counts, "code_execution": result.code_execution}
                    task.progress = int((index + 1) / max(len(sources), 1) * 95)
                    task.heartbeat_at = now()
                    db.commit()
                save_event(identifier, f"来源完成：{len(result.records)} 条，覆盖 {result.coverage}", expected_attempt=expected_attempt)
            except Exception as exc:
                code = exc.code if isinstance(exc, CollectionError) else type(exc).__name__
                message = exc.message if isinstance(exc, CollectionError) else "来源运行异常，请查看适配状态"
                with session_factory()() as db:
                    try:
                        active_execution(db, identifier, expected_attempt, allow_cancel=True)
                    except CollectionError:
                        return
                    source_run = db.get(SourceRun, source_run_id)
                    source_run.status = code if code in {
                        "waiting_login", "waiting_verification", "waiting_account", "waiting_device", "cancelled"
                    } else "failed"
                    if source_run.status == "failed" and (source_run.counts or {}).get("observed", 0) > 0:
                        source_run.status, source_run.coverage = "partial", "partial"
                    source_run.error_code, source_run.error_message = code, message
                    if getattr(exc, "code_execution", None):
                        source_run.counts = {**(source_run.counts or {}), "code_execution": exc.code_execution}
                    binding = db.get(SourceBinding, binding_id)
                    if binding:
                        binding.health = code
                    db.add(TaskEvent(task_id=identifier, message=message, level="warning",
                                     detail={"source": snapshot["kind"], "code": code}))
                    if isinstance(exc, CollectionError) and not filters.get("repair_recovery"):
                        try:
                            with db.begin_nested():
                                from app.adapter_code_orchestration import record_code_failure, queue_code_from_failure
                                code_incident = record_code_failure(db, identifier, source_run_id, snapshot, code,
                                    getattr(exc, "code_failure_evidence", None) or getattr(exc, "repair_evidence", None),
                                    getattr(exc, "adapter_fingerprint", ""))
                                if code_incident is not None:
                                    queue_code_from_failure(db, code_incident.id)
                                else:
                                    from app.auto_maintenance import queue_repair_incident
                                    queue_repair_incident(db, identifier, source_run_id, snapshot, code,
                                                          getattr(exc, "repair_evidence", None),
                                                          getattr(exc, "adapter_fingerprint", ""))
                        except Exception as repair_error:
                            db.add(TaskEvent(task_id=identifier, level="warning",
                                             message="未建立自动维护事件，已保留原始来源失败",
                                             detail={"code": type(repair_error).__name__}))
                    if code in {"waiting_login", "waiting_verification", "adaptation_required"}:
                        from app.notifications import emit_event
                        company = db.get(Company, task.company_id)
                        emit_event(db, event_type="source.attention", dedupe_key=f"source:{source_run_id}:{code}",
                                   title=company.name + " · 来源需要处理", text=message,
                                   subject_id=company.subject_id, task_id=identifier,
                                   url=get_settings().app_url.rstrip("/") + "/sources")
                    db.commit()
                if code in {"cancelled", "time_budget"}:
                    break
    except CollectionError as exc:
        with session_factory()() as db:
            try:
                task = active_execution(db, identifier, expected_attempt, allow_cancel=True)
            except CollectionError:
                return
            task.error_code, task.error_message = exc.code, exc.message
            db.commit()
    finally:
        with session_factory()() as db:
            try:
                task = active_execution(db, identifier, expected_attempt, allow_cancel=True)
            except CollectionError:
                return
            runs = db.scalars(select(SourceRun).where(SourceRun.task_id == identifier)).all()
            states = [s.status for s in runs]
            valid = [s for s in runs if s.status in {"succeeded", "partial"}]
            if task.cancel_requested:
                task.status = "cancelled"
            elif len(runs) == len(sources) and states and all(s == "succeeded" for s in states):
                task.status = "succeeded"
            elif valid:
                task.status = "partial"
            elif "waiting_login" in states:
                task.status = "waiting_login"
            elif "waiting_verification" in states:
                task.status = "waiting_verification"
            elif "waiting_account" in states:
                task.status = "waiting_account"
            elif "waiting_device" in states:
                task.status = "waiting_device"
            else:
                task.status = "failed"
            issues = [source for source in runs if source.error_message]
            if issues and task.status not in {"succeeded", "cancelled"} and not task.error_message:
                task.error_code = issues[0].error_code
                task.error_message = "；".join(source.error_message for source in issues)[:2000]
            totals = {}
            for source in valid:
                for key, value in source.counts.items():
                    if type(value) is int:
                        totals[key] = totals.get(key, 0) + value
            task.summary = {"counts": totals, "sources": len(runs), "valid_sources": len(valid)}
            task.progress, task.finished_at = 100, now()
            company_run = db.get(CompanyRun, identifier)
            company_run.coverage = "complete" if task.status == "succeeded" else "partial" if valid else "unknown"
            db.add(TaskEvent(task_id=identifier, message="本次采集结束",
                             detail={"status": task.status, "summary": task.summary}))
            if filters.get("repair_recovery"):
                if filters.get("code_recovery"):
                    from app.adapter_code_orchestration import finish_code_recovery
                    finish_code_recovery(db, identifier)
                else:
                    from app.auto_maintenance import finish_recovery
                    finish_recovery(db, identifier)
            db.commit()


@celery.task(name="app.analyze_company", soft_time_limit=180, time_limit=210)
def analyze_company(task_id: str):
    from app.agents import company_analysis_graph
    from app.llm import ModelError
    identifier = uuid.UUID(task_id)
    with session_factory()() as db:
        claimed = db.execute(update(TaskRun).where(
            TaskRun.id == identifier, TaskRun.status == "queued",
            TaskRun.cancel_requested.is_(False),
        ).values(status="running", started_at=now(), heartbeat_at=now(),
                 attempt=TaskRun.attempt + 1).returning(TaskRun.company_id)).first()
        if not claimed:
            return
        company_id = claimed[0]
        db.commit()


    try:
        company_analysis_graph.invoke({"task_id": str(identifier), "company_id": str(company_id)})
        final, error_code, error_message = "succeeded", None, None
    except ModelError as exc:
        final = "cancelled" if exc.code == "cancelled" else "failed"
        error_code, error_message = exc.code, exc.message
    except Exception as exc:
        final, error_code, error_message = "failed", type(exc).__name__, "分析未完成，请查看任务记录"
    with session_factory()() as db:
        task = db.get(TaskRun, identifier)
        task.status = "cancelled" if task.cancel_requested else final
        task.error_code, task.error_message = error_code, error_message
        task.progress, task.finished_at = 100, now()
        db.add(TaskEvent(task_id=identifier, level="info" if final == "succeeded" else "warning",
                         message=error_message or "公司分析报告已生成"))
        db.commit()


@celery.task(name="app.run_sentiment", soft_time_limit=700, time_limit=750)
def run_sentiment(task_id: str):
    from app.sentiment_service import execute_sentiment_task
    execute_sentiment_task(task_id)


@celery.task(name="app.deliver_notifications")
def deliver_notifications():
    from app.news_policy_reminders import queue_due_policy_reminders
    from app.notification_digest import queue_due_digests
    from app.notifications import process_outbox
    queue_due_policy_reminders(limit=20)
    queue_due_digests()
    return process_outbox()


@celery.task(name="app.run_news", soft_time_limit=700, time_limit=750)
def run_news(task_id: str):
    from app.news_service import execute_news_task
    execute_news_task(task_id)


@celery.task(name="app.run_retention", soft_time_limit=240, time_limit=300)
def run_retention(run_id: str | None = None):
    from app.retention_service import run_retention as execute_retention
    return execute_retention(trigger="manual" if run_id else "scheduled", limit=100, run_id=run_id)


@celery.task(name="app.compare_companies", soft_time_limit=180, time_limit=210)
def compare_companies(task_id: str):
    from app.analytics_service import execute_comparison_task
    execute_comparison_task(task_id)


@celery.task(name="app.dispatch_maintenance")
def dispatch_maintenance():
    from app.auto_maintenance import reconcile_repair_incidents
    from app.auto_maintenance_models import AdapterRepairIncident
    from app.adapter_maintenance import enqueue_due_checks
    from app.maintenance_models import AdapterMaintenanceRun, AdapterRegistry
    from app.adapter_code_models import AdapterCodeProposal
    from app.adapter_code_orchestration import GENERATING, VALIDATING, code_tick_due, reconcile_code_recoveries
    with session_factory()() as db:
        enqueue_due_checks(db)
        reconcile_repair_incidents(db)
        reconcile_code_recoveries(db)
        stale = db.scalars(select(AdapterMaintenanceRun).where(
            AdapterMaintenanceRun.status == "running",
            AdapterMaintenanceRun.started_at < now() - timedelta(minutes=10),
        )).all()
        for run in stale:
            run.status, run.error_code = "failed", "worker_interrupted"
            run.error_message, run.finished_at = "维护任务中断，保留当前发布版本", now()
        db.commit()
        ids = db.scalars(select(AdapterMaintenanceRun.id).where(
            AdapterMaintenanceRun.status == "queued", AdapterMaintenanceRun.kind == "upstream_check",
        ).order_by(AdapterMaintenanceRun.created_at).limit(10)).all()
        repairs = db.scalars(select(AdapterRepairIncident.id).where(
            AdapterRepairIncident.status == "queued", AdapterRepairIncident.cancel_requested.is_(False),
        ).order_by(AdapterRepairIncident.created_at).limit(5)).all()
        # Retry dispatch, not external work, when the worker stopped after the upstream check commit.
        upstream = db.scalars(select(AdapterMaintenanceRun.id).join(
            AdapterRegistry, AdapterRegistry.id == AdapterMaintenanceRun.registry_id).where(
            AdapterMaintenanceRun.kind == "upstream_check",
            AdapterMaintenanceRun.status == "needs_engineering",
            AdapterMaintenanceRun.details["code_handled"].as_boolean().is_distinct_from(True),
            AdapterRegistry.code_agent_enabled.is_(True), AdapterRegistry.code_upstream_enabled.is_(True),
        ).order_by(AdapterMaintenanceRun.created_at).limit(10)).all()
        proposals = db.execute(select(AdapterCodeProposal, AdapterRegistry).join(
            AdapterRegistry, AdapterRegistry.id == AdapterCodeProposal.registry_id).where(
            AdapterRegistry.code_agent_enabled.is_(True), AdapterCodeProposal.cancel_requested.is_(False),
            AdapterCodeProposal.source_snapshot["automatic"].as_boolean().is_(True),
            or_(AdapterCodeProposal.origin != "fixed_upstream", AdapterRegistry.code_upstream_enabled.is_(True)),
            or_(AdapterCodeProposal.status.in_(GENERATING | VALIDATING | {"validated", "generating"}),
                and_(AdapterCodeProposal.status == "awaiting_publish", AdapterRegistry.code_auto_publish_enabled.is_(True)),
                and_(AdapterCodeProposal.status == "published", AdapterCodeProposal.source_snapshot["incident_id"].astext.is_not(None))),
        ).order_by(AdapterCodeProposal.updated_at).limit(10)).all()
        code_ids = [proposal.id for proposal, registry in proposals if code_tick_due(proposal, registry)]
    for identifier in ids:
        check_adapter.apply_async(args=[str(identifier)], queue="maintenance")
    for identifier in repairs:
        repair_adapter.apply_async(args=[str(identifier)], queue="maintenance")
    for identifier in upstream:
        queue_adapter_code_upstream.apply_async(args=[str(identifier)], queue="maintenance")
    for identifier in code_ids:
        maintain_adapter_code.apply_async(args=[str(identifier)], queue="maintenance")


@celery.task(name="app.check_adapter", soft_time_limit=180, time_limit=210)
def check_adapter(run_id: str):
    from app.adapter_maintenance import execute_check
    execute_check(run_id)
    queue_adapter_code_upstream.apply_async(args=[run_id], queue="maintenance")


@celery.task(name="app.repair_adapter", soft_time_limit=400, time_limit=430)
def repair_adapter(incident_id: str):
    from app.auto_maintenance import execute_repair_incident
    execute_repair_incident(incident_id)


@celery.task(name="app.queue_adapter_code_upstream", soft_time_limit=180, time_limit=210)
def queue_adapter_code_upstream(run_id: str):
    from app.adapter_code_orchestration import queue_code_from_upstream
    lease = redis_client().lock("code-upstream:" + run_id, timeout=240, blocking=False, thread_local=False)
    if not lease.acquire():
        return
    try:
        with session_factory()() as db:
            queue_code_from_upstream(db, run_id)
            db.commit()
    finally:
        try:
            lease.release()
        except redis.exceptions.LockError:
            pass


@celery.task(name="app.maintain_adapter_code", soft_time_limit=700, time_limit=750)
def maintain_adapter_code(proposal_id: str):
    from app.adapter_code_orchestration import tick_code_maintenance
    lease = redis_client().lock("code-maintenance:" + proposal_id, timeout=800, blocking=False, thread_local=False)
    if not lease.acquire():
        return
    try:
        tick_code_maintenance(proposal_id)
    finally:
        try:
            lease.release()
        except redis.exceptions.LockError:
            pass
