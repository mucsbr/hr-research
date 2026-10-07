"""Automatic code-maintenance events, shared budgets and one-source recovery."""
from copy import deepcopy
from datetime import timedelta
import json
import re
import uuid

from sqlalchemy import func, select, text

from app.adapter_code_catalog import catalog_for, failure_case
from app.adapter_code_models import AdapterCodePackage, AdapterCodeProposal
from app.adapter_code_package import content_sha256, load_bundle
from app.adapter_code_service import create_code_proposal
from app.adapter_code_upstream import FIXED_CODE_UPSTREAMS, sample_upstream_diff
from app.adapter_maintenance import activate_release, ensure_registry
from app.auto_maintenance_models import AdapterRepairIncident
from app.collection import CollectionError
from app.config import get_settings
from app.db import session_factory
from app.maintenance_models import AdapterMaintenanceRun, AdapterRegistry, AdapterRelease
from app.models import Company, CompanyRun, JobObservation, JobSourceRecord, ModelCall, RunBatch, SourceAccount, SourceBinding, SourceRun, Subject, TaskEvent, TaskRun, User, now

REPAIR_CODES = {"source_contract_changed", "selector_mismatch", "adapter_program_invalid", "runner_execution_failed", "adaptation_required"}
VALIDATING = {"draft_ready", "resolving", "resolved", "uploading", "validating"}
GENERATING = {"queued", "retry_ready", "blocked_configuration"}
STOPPED = {"failed", "cancelled", "conflict", "unknown", "skipped", "recovered"}
INFRA_MODEL = {"not_configured", "daily_budget", "task_budget", "input_budget", "stream_interrupted", "output_truncated", "ReadTimeout", "ConnectError"}


def _kind(value):
    return "official" if value == "careers" else value


def _stop_incident(incident, reason):
    incident.status = "needs_engineering"
    incident.details = {**incident.details, "code_error": reason}


def _scope(db, incident, *, lock=False):
    def current(model, identifier):
        if not lock:
            return db.get(model, identifier, populate_existing=True)
        # Registry -> incident -> original Task -> Binding -> Company -> Subject.
        # Workers can already own Task -> Binding; never wait while owning registry.
        row = db.scalar(select(model).where(model.id == identifier).with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True))
        if not row:
            raise CollectionError("source_configuration_busy", "来源配置正在变更或已移除，未提交代码发布或恢复")
        return row
    original = current(TaskRun, incident.task_id)
    source = db.get(SourceRun, incident.source_run_id, populate_existing=True)
    binding = current(SourceBinding, incident.binding_id)
    frozen = deepcopy(incident.details.get("binding_snapshot") or {})
    if not original or not source or source.task_id != original.id or not binding or binding.company_id != original.company_id:
        raise CollectionError("source_configuration_changed", "原公司与来源运行不再对应")
    if original.cancel_requested or original.status == "cancelled" or incident.cancel_requested:
        raise CollectionError("cancelled", "原采集或维护事件已取消")
    company = current(Company, original.company_id)
    if not company or company.archived or not binding.enabled:
        raise CollectionError("source_configuration_changed", "公司或来源已停用")
    if any(str(getattr(binding, key) or "") != str(frozen.get(key) or "")
           for key in ("entry_url", "account_id", "external_company_id", "kind", "company_id", "adapter")):
        raise CollectionError("source_configuration_changed", "来源网址、账号或身份已变化")
    identity = deepcopy(original.config_snapshot.get("company_identity") or {"name": original.config_snapshot.get("company_name"), "aliases": []})
    subject = current(Subject, company.subject_id)
    if not subject or not subject.active or identity.get("name") != company.name or (identity.get("official_name") or None) != (company.official_name or None) or identity.get("aliases", []) != (subject.aliases or []):
        raise CollectionError("source_configuration_changed", "公司身份配置已变化")
    old_config, current_config = frozen.get("config") or {}, binding.config or {}
    if old_config != current_config:
        allowed = {"website_plan", "plan_hash", "plan_call_id", "plan_model"}
        if binding.kind != "careers" or {key: value for key, value in old_config.items() if key not in allowed} != {
            key: value for key, value in current_config.items() if key not in allowed
        }:
            raise CollectionError("source_configuration_changed", "来源配置已变化")
        from app.checkpoint_models import OfficialCheckpoint
        checkpoint = db.scalar(select(OfficialCheckpoint).where(OfficialCheckpoint.task_id == original.id,
                               OfficialCheckpoint.binding_id == binding.id))
        if not checkpoint or not checkpoint.plan or checkpoint.plan != current_config.get("website_plan"):
            raise CollectionError("source_configuration_changed", "官网配置变化不是本次第一方验证计划")
        frozen["config"] = deepcopy(current_config)
    if binding.kind != "careers":
        account = current(SourceAccount, binding.account_id) if binding.account_id else None
        if account and (account.kind != binding.kind or account.health not in {"saved", "ready"}):
            raise CollectionError("waiting_login", "来源账号已失效，不用代码修复替代登录")
        if not account and binding.kind not in {"zhaopin", "51job"}:
            raise CollectionError("waiting_login", "来源没有可用登录会话")
    return original, source, binding, frozen, identity, company


def _locked_incident(db, identifier):
    initial = db.get(AdapterRepairIncident, uuid.UUID(str(identifier)))
    if not initial:
        return None, None
    registry = db.scalar(select(AdapterRegistry).where(AdapterRegistry.id == initial.registry_id)
                         .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if not registry:
        return None, None
    incident = db.scalar(select(AdapterRepairIncident).where(AdapterRepairIncident.id == initial.id)
                         .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    return incident, registry


def record_code_failure(db, task_id, source_run_id, binding_snapshot, error_code, evidence, base_fingerprint):
    if error_code not in REPAIR_CODES or not isinstance(binding_snapshot, dict):
        return None
    task = db.get(TaskRun, task_id)
    source = db.get(SourceRun, source_run_id)
    if not task or not source or source.task_id != task.id or task.config_snapshot.get("repair_recovery") or task.cancel_requested:
        return None
    frozen = next((item for item in task.config_snapshot.get("sources_snapshot", []) if item.get("id") == str(source.binding_id)), None)
    if frozen != binding_snapshot or str(task.company_id) != frozen.get("company_id"):
        return None
    registry = ensure_registry(db, _kind(frozen["kind"]))
    if not registry.code_agent_enabled:
        return None
    key = content_sha256({"source_run_id": str(source.id), "base_fingerprint": base_fingerprint})
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "repair:" + key})
    old = db.scalar(select(AdapterRepairIncident).where(AdapterRepairIncident.dedupe_key == key))
    if old:
        return old
    current = db.get(AdapterRelease, registry.active_release_id)
    if not current or current.fingerprint != base_fingerprint:
        return None
    safe = deepcopy(evidence) if isinstance(evidence, dict) and len(json.dumps(evidence, ensure_ascii=False).encode()) <= 64000 else {}
    incident = AdapterRepairIncident(registry_id=registry.id, task_id=task.id, source_run_id=source.id,
        binding_id=source.binding_id, status="code_pending", error_code=error_code, base_fingerprint=base_fingerprint,
        evidence_hash=content_sha256(safe), dedupe_key=key, evidence=safe,
        details={"binding_snapshot": deepcopy(frozen), "base_release_id": str(current.id), "base_generation": registry.release_generation})
    db.add(incident)
    db.flush()
    return incident


def _base_spec(db, release, catalog):
    program = release.manifest.get("program")
    if not program:
        return catalog["base_spec"]
    package = db.scalar(select(AdapterCodePackage).where(AdapterCodePackage.bundle_sha == program["bundle_sha256"]))
    if not package:
        raise CollectionError("code_package_missing", "当前生产代码包缺失，不能用其他基线生成候选")
    bundle = load_bundle(package.file_ref, expected_sha256=package.bundle_sha)
    return {key: value for key, value in bundle.items() if key != "lock"}


def queue_code_from_failure(db, incident_id):
    incident, registry = _locked_incident(db, incident_id)
    if not incident or not registry.code_agent_enabled:
        return None
    existing = incident.details.get("code_proposal_id")
    if existing:
        return db.get(AdapterCodeProposal, uuid.UUID(existing))
    if incident.status not in {"queued", "code_pending", "needs_engineering"} or incident.error_code not in REPAIR_CODES:
        return None
    try:
        original, _, _, binding, identity, _ = _scope(db, incident)
        release = db.get(AdapterRelease, registry.active_release_id)
        if not release or release.fingerprint != incident.base_fingerprint:
            raise CollectionError("release_conflict", "原解析失败的适配版本已变化")
        calls = db.scalars(select(ModelCall).where(ModelCall.task_id == incident.maintenance_task_id)).all() if incident.maintenance_task_id else []
        if any(call.status != "completed" for call in calls) or incident.details.get("last_error") in INFRA_MODEL:
            raise CollectionError("model_outcome_unavailable", "原模型结果未知或基础设施失败，不生成代码候选")
        consumed = max(incident.attempts, len(calls))
        if consumed >= 2:
            raise CollectionError("candidate_budget", "同一原始事件的JSON与代码两次候选预算已用完")
        catalog = catalog_for(registry.kind, release.manifest)
        case = failure_case(registry.kind, binding, identity, incident.evidence, release.manifest)
        shared = db.get(TaskRun, incident.maintenance_task_id) if incident.maintenance_task_id else None
        if shared is None:
            shared = TaskRun(domain="maintenance", kind="adapter_code_upgrade", status="queued", scope_hash=incident.base_fingerprint,
                             config_snapshot={"repair_incident_id": str(incident.id), "max_candidates": 2})
            db.add(shared)
            db.flush()
            incident.maintenance_task_id = shared.id
        source = {"automatic": True, "incident_id": str(incident.id), "original_task_id": str(original.id),
                  "source_run_id": str(incident.source_run_id), "catalog_sha256": catalog["catalog_sha256"],
                  "evidence_sha256": incident.evidence_hash, "kind": registry.kind}
        proposal = create_code_proposal(db, registry.kind, base_spec=_base_spec(db, release, catalog), source_snapshot=source,
                    evidence_cases=[*catalog["evidence_cases"], case], origin="failure_evidence", request_key="incident:" + str(incident.id),
                    max_candidates=2, shared_task_id=shared.id, existing_attempts=consumed)
        incident.status, incident.attempts = "code_queued", consumed
        incident.details = {**incident.details, "code_proposal_id": str(proposal.id), "budget_owner_task_id": str(shared.id)}
        return proposal
    except CollectionError as exc:
        _stop_incident(incident, exc.code)
        return None


def queue_code_from_upstream(db, check_run_id, *, fetcher=None):
    check = db.get(AdapterMaintenanceRun, uuid.UUID(str(check_run_id)))
    if not check or check.kind != "upstream_check" or check.status not in {"needs_engineering", "code_sampled"}:
        return None
    registry = db.get(AdapterRegistry, check.registry_id)
    if not registry or not registry.code_agent_enabled or not registry.code_upstream_enabled:
        return None
    repository = FIXED_CODE_UPSTREAMS.get(registry.kind)
    target = check.upstream_commit or check.details.get("observed_commit")
    base = registry.upstream_commit
    if not repository or not re.fullmatch(r"[0-9a-f]{40}", str(target or "")) or not re.fullmatch(r"[0-9a-f]{40}", str(base or "")) or base == target:
        check.details = {**check.details, "code_handled": True, "code_reason": "upstream_not_registered_or_no_valid_change"}
        return None
    event_key = content_sha256({"registry": str(registry.id), "base_release": str(registry.active_release_id),
                               "generation": registry.release_generation, "target": target})
    previous = db.scalar(select(AdapterCodeProposal).where(AdapterCodeProposal.registry_id == registry.id,
                         AdapterCodeProposal.source_snapshot["upstream_event_key"].astext == event_key))
    if previous:
        check.details = {**check.details, "code_proposal_id": str(previous.id), "code_handled": True}
        return previous
    original_id, generation = registry.active_release_id, registry.release_generation
    try:
        sampled = sample_upstream_diff(registry.kind, base, target, fetcher=fetcher)
        if not any(item.get("diff") for item in sampled["files"]):
            check.details = {**check.details, "code_handled": True, "code_reason": "no_relevant_code_change"}
            return None
        registry = db.scalar(select(AdapterRegistry).where(AdapterRegistry.id == registry.id)
                             .with_for_update().execution_options(populate_existing=True))
        if not registry.code_agent_enabled or not registry.code_upstream_enabled or registry.active_release_id != original_id or registry.release_generation != generation or registry.upstream_commit != base:
            raise CollectionError("release_conflict", "采样期间生产版本、开关或上游基线变化")
        release = db.get(AdapterRelease, original_id)
        catalog = catalog_for(registry.kind, release.manifest)
        source = {**sampled, "automatic": True, "upstream_event_key": event_key, "catalog_sha256": catalog["catalog_sha256"]}
        proposal = create_code_proposal(db, registry.kind, base_spec=_base_spec(db, release, catalog), source_snapshot=source,
                    evidence_cases=catalog["evidence_cases"], origin="fixed_upstream", request_key="upstream:" + event_key, max_candidates=2)
        check.details = {**check.details, "code_proposal_id": str(proposal.id), "code_handled": True}
        return proposal
    except CollectionError as exc:
        check.details = {**check.details, "code_handled": True, "code_error": exc.code}
        return None


queue_code_from_check = queue_code_from_upstream


def code_tick_due(proposal, registry):
    """Pure dispatch predicate. No DB queries; settled published history is excluded."""
    if not registry or not registry.code_agent_enabled or proposal.cancel_requested or not proposal.source_snapshot.get("automatic"):
        return False
    if proposal.origin == "fixed_upstream" and not registry.code_upstream_enabled:
        return False
    if proposal.status == "published":
        return bool(proposal.source_snapshot.get("incident_id"))
    if proposal.status == "awaiting_publish":
        return registry.code_auto_publish_enabled
    return proposal.status in GENERATING | VALIDATING | {"validated", "generating"}


def _manual_actor(db, identifier):
    if not identifier:
        return None
    try:
        actor = db.get(User, uuid.UUID(str(identifier)), populate_existing=True)
    except (ValueError, TypeError):
        return None
    return actor if actor and actor.active and actor.role == "admin" else None


def on_code_published(db, proposal, release, *, manual_actor_id=None):
    """Called inside the single publication transaction, also safe to replay once."""
    incident_id = proposal.source_snapshot.get("incident_id")
    if not proposal.source_snapshot.get("automatic") or not incident_id:
        return None
    incident, registry = _locked_incident(db, incident_id)
    if not incident or not registry:
        raise CollectionError("code_event_busy", "原失败事件正在其他维护阶段，未发布并行版本")
    if incident.recovery_task_id:
        proposal.status = "recovery_queued"
        return db.get(TaskRun, incident.recovery_task_id)
    if incident.recovery_attempts:
        raise CollectionError("recovery_budget_exhausted", "本原始事件的恢复机会已使用")
    actor = _manual_actor(db, manual_actor_id)
    if not registry.code_agent_enabled or not (registry.code_auto_publish_enabled or actor):
        raise CollectionError("maintenance_agent_disabled", "自动代码发布或恢复已关闭")
    original, _, _, binding, _, company = _scope(db, incident, lock=True)
    if registry.active_release_id != release.id or release.id != proposal.release_id:
        raise CollectionError("release_conflict", "当前活动版本不对应本次已发布代码")
    config = deepcopy(original.config_snapshot)
    config.pop("adapter_versions", None)
    config.pop("resume_from_task_id", None)
    source = {**binding, "expected_adapter_fingerprint": release.fingerprint,
              "expected_release_id": str(release.id), "expected_release_generation": registry.release_generation}
    config.update(sources_snapshot=[source], sources=[binding["kind"]], repair_recovery=True, code_recovery=True,
                  repair_incident_id=str(incident.id), code_proposal_id=str(proposal.id))
    config["code_manual_publish_actor_id"] = str(actor.id) if actor else None
    batch = RunBatch(name=company.name + " · 代码修复后恢复", trigger="code_maintenance_recovery",
                     idempotency_key="code-recovery:" + str(incident.id), config_snapshot=config)
    db.add(batch)
    db.flush()
    recovery = TaskRun(batch_id=batch.id, company_id=original.company_id, domain="recruitment", kind="company_collection",
                       config_snapshot=config, scope_hash=original.scope_hash, status="queued")
    db.add(recovery)
    db.flush()
    db.add(CompanyRun(task_run_id=recovery.id, company_id=original.company_id, scope_hash=original.scope_hash))
    db.add(TaskEvent(task_id=recovery.id, message="已验证代码发布，仅恢复原失败来源一次",
                    detail={"code_proposal_id": str(proposal.id), "release_id": str(release.id), "generation": registry.release_generation}))
    incident.release_id, incident.recovery_task_id, incident.recovery_attempts = release.id, recovery.id, 1
    incident.status, proposal.status = "code_recovery_queued", "recovery_queued"
    incident.details = {**incident.details, "published_generation": registry.release_generation,
                        "published_fingerprint": release.fingerprint, "code_proposal_id": str(proposal.id),
                        "manual_publish_actor_id": str(actor.id) if actor else None}
    task = db.get(TaskRun, proposal.task_id)
    task.config_snapshot = {**task.config_snapshot, "code_orchestration": {"recovery_task_id": str(recovery.id), "settled": True}}
    return recovery


def _preflight(db, proposal):
    registry = db.get(AdapterRegistry, proposal.registry_id, populate_existing=True)
    if not registry.code_agent_enabled or proposal.cancel_requested:
        raise CollectionError("maintenance_agent_disabled", "代码维护已关闭或取消")
    if proposal.origin == "fixed_upstream" and not registry.code_upstream_enabled:
        raise CollectionError("maintenance_agent_disabled", "固定上游自动维护已关闭")
    incident_id = proposal.source_snapshot.get("incident_id")
    if incident_id:
        incident = db.get(AdapterRepairIncident, uuid.UUID(incident_id), populate_existing=True)
        if not incident:
            raise CollectionError("incident_missing", "原始失败事件不存在")
        _scope(db, incident)
        consumed = db.scalar(select(func.count()).select_from(ModelCall).where(ModelCall.task_id == proposal.task_id)) or 0
        if max(incident.attempts, consumed) > proposal.attempts:
            proposal.attempts = max(incident.attempts, consumed)
        if any(call.status in {"unknown", "reserved"} for call in db.scalars(select(ModelCall).where(ModelCall.task_id == proposal.task_id))) and proposal.status in GENERATING:
            raise CollectionError("model_outcome_unavailable", "共用预算中的模型结果未确定，不重复生成")
    return registry


def _sync_incident(db, proposal):
    identifier = proposal.source_snapshot.get("incident_id")
    if not identifier:
        return
    incident = db.get(AdapterRepairIncident, uuid.UUID(identifier))
    if not incident:
        return
    incident.attempts = max(incident.attempts, proposal.attempts)
    if proposal.status in STOPPED:
        incident.status = "cancelled" if proposal.status == "cancelled" else "needs_engineering"
        incident.details = {**incident.details, "code_error": proposal.error_code}
    elif proposal.status not in {"published", "recovery_queued", "recovering"}:
        incident.status = "code_awaiting_publish" if proposal.status in {"validated", "awaiting_publish"} else "code_validating" if proposal.status in VALIDATING else "code_running"


def tick_code_maintenance(proposal_id, *, client=None, gateway=None):
    from app.adapter_code_release import create_release_from_proposal, publish_code_proposal
    from app.adapter_code_service import advance_code_validation, generate_code_candidate
    from app.adapter_runner_client import AdapterRunnerClient
    identifier = uuid.UUID(str(proposal_id))
    client = client or AdapterRunnerClient()
    with session_factory()() as db:
        proposal = db.get(AdapterCodeProposal, identifier)
        if not proposal or proposal.status in STOPPED | {"recovery_queued", "recovering"}:
            return {"proposal_id": str(identifier), "status": proposal.status if proposal else "missing"}
        try:
            registry = _preflight(db, proposal)
            state = proposal.status
            if state == "generating":
                task = db.get(TaskRun, proposal.task_id)
                if task.heartbeat_at and task.heartbeat_at < now() - timedelta(seconds=get_settings().llm_timeout_seconds + 60):
                    proposal.status, proposal.error_code, task.status = "unknown", "generation_interrupted", "failed"
                    _sync_incident(db, proposal)
                    db.commit()
                return {"proposal_id": str(identifier), "status": proposal.status}
            if state in {"validated", "awaiting_publish"}:
                create_release_from_proposal(db, proposal.id, client=client)
                if registry.code_auto_publish_enabled:
                    release = publish_code_proposal(db, proposal.id, expected_active_release_id=proposal.base_release_id,
                              expected_generation=proposal.base_generation, client=client)
                    on_code_published(db, proposal, release)
                _sync_incident(db, proposal)
                db.commit()
                return {"proposal_id": str(identifier), "status": proposal.status}
            if state == "published":
                on_code_published(db, proposal, db.get(AdapterRelease, proposal.release_id))
                db.commit()
                return {"proposal_id": str(identifier), "status": proposal.status}
            db.commit()
        except CollectionError as exc:
            db.rollback()
            proposal = db.get(AdapterCodeProposal, identifier)
            if exc.code in {"source_configuration_busy", "code_event_busy"}:
                # A concurrent editor did not invalidate the tested candidate.
                # Retry this stage only; never allocate another model attempt.
                proposal.error_code = exc.code
                db.commit()
                return {"proposal_id": str(identifier), "status": proposal.status, "error_code": exc.code}
            proposal.status = "cancelled" if exc.code in {"cancelled", "maintenance_agent_disabled"} else "conflict" if exc.code == "release_conflict" else "failed"
            proposal.error_code = exc.code
            _sync_incident(db, proposal)
            db.commit()
            return {"proposal_id": str(identifier), "status": proposal.status, "error_code": exc.code}
    if state in GENERATING:
        generate_code_candidate(identifier, gateway=gateway, client=client)
    elif state in VALIDATING:
        advance_code_validation(identifier, client=client)
    with session_factory()() as db:
        proposal = db.get(AdapterCodeProposal, identifier)
        _sync_incident(db, proposal)
        db.commit()
        return {"proposal_id": str(identifier), "status": proposal.status, "error_code": proposal.error_code}


def prepare_code_recovery(db, task_id):
    task = db.get(TaskRun, task_id)
    if not task or not task.config_snapshot.get("code_recovery"):
        return
    incident, registry = _locked_incident(db, task.config_snapshot["repair_incident_id"])
    if not incident or incident.recovery_task_id != task.id or task.attempt > 1 or incident.status != "code_recovery_queued":
        raise CollectionError("recovery_budget_exhausted", "代码维护只允许一次原来源恢复")
    _scope(db, incident, lock=True)
    manual_id = task.config_snapshot.get("code_manual_publish_actor_id")
    authorized = manual_id and manual_id == incident.details.get("manual_publish_actor_id") and _manual_actor(db, manual_id)
    if task.cancel_requested or not registry.code_agent_enabled or not (registry.code_auto_publish_enabled or authorized):
        raise CollectionError("cancelled", "代码恢复已取消或关闭")
    frozen = task.config_snapshot["sources_snapshot"][0]
    if str(registry.active_release_id) != frozen["expected_release_id"] or registry.release_generation != frozen["expected_release_generation"]:
        raise CollectionError("adapter_version_conflict", "代码恢复前活动版本或代次已变化")
    incident.status = "code_recovering"
    proposal = db.get(AdapterCodeProposal, uuid.UUID(task.config_snapshot["code_proposal_id"]))
    proposal.status = "recovering"


def finish_code_recovery(db, task_id, *, client=None):
    task = db.get(TaskRun, task_id, populate_existing=True)
    if not task or not task.config_snapshot.get("code_recovery"):
        return None
    incident, registry = _locked_incident(db, task.config_snapshot["repair_incident_id"])
    if not incident or incident.recovery_task_id != task.id or incident.status not in {"code_recovery_queued", "code_recovering"}:
        return incident
    proposal = db.get(AdapterCodeProposal, uuid.UUID(task.config_snapshot["code_proposal_id"]))
    if incident.cancel_requested or task.cancel_requested or task.status == "cancelled":
        incident.status = proposal.status = "cancelled"
        return incident
    release = db.get(AdapterRelease, incident.release_id)
    source = db.scalar(select(SourceRun).where(SourceRun.task_id == task.id, SourceRun.binding_id == incident.binding_id))
    proof = (source.counts or {}).get("code_execution", {}) if source else {}
    program = release.manifest.get("program", {}) if release else {}
    required = {"release_id": str(release.id), "release_generation": incident.details.get("published_generation"),
                "fingerprint": release.fingerprint, **{key: program.get(key) for key in ("bundle_sha256", "lock_sha256", "runtime_sha256")}} if release else {}
    verified = bool(required and all(proof.get(key) == value for key, value in required.items()) and proof.get("verified") is True and proof.get("run_ids"))
    if verified:
        from app.adapter_runtime_models import AdapterProgramExecution
        from app.adapter_runner_client import AdapterRunnerClient
        verifier = client or AdapterRunnerClient()
        identifiers = proof["run_ids"]
        verified = (isinstance(identifiers, list) and 0 < len(identifiers) <= 500
                    and all(isinstance(value, str) for value in identifiers) and len(set(identifiers)) == len(identifiers))
        seen, operations = set(), set()
        rows = db.scalars(select(AdapterProgramExecution).where(AdapterProgramExecution.remote_run_id.in_(identifiers))).all() if verified else []
        for execution in rows:
            if (execution.remote_run_id in seen or execution.status != "accepted" or execution.task_id != task.id
                    or execution.source_run_id != source.id or execution.binding_id != incident.binding_id
                    or execution.owner_attempt != task.attempt or execution.adapter_fingerprint != release.fingerprint
                    or execution.bundle_sha != program["bundle_sha256"] or execution.request_sha != content_sha256(execution.request)
                    or execution.input_sha != content_sha256(execution.request.get("input"))
                    or execution.request.get("bundle_sha256") != program["bundle_sha256"]
                    or execution.operation != execution.request.get("operation")
                    or execution.result.get("run_id") != execution.remote_run_id):
                verified = False
                break
            try:
                verifier.verify_result(execution.result, request=execution.request, lock_sha256=program["lock_sha256"],
                                       expected_runtime=program["runtime_sha256"])
            except CollectionError:
                verified = False
                break
            if execution.result.get("receipt", {}).get("dependency_environment_sha256") != program.get("dependency_environment_sha256"):
                verified = False
                break
            seen.add(execution.remote_run_id)
            operations.add(execution.operation)
        verified = verified and seen == set(identifiers) and {"parse_list", "parse_detail"}.issubset(operations)
    observed = db.scalar(select(func.count()).select_from(JobObservation).join(JobSourceRecord, JobSourceRecord.id == JobObservation.record_id)
                         .where(JobObservation.source_run_id == source.id, JobSourceRecord.binding_id == incident.binding_id)) if source else 0
    if source and source.status in {"succeeded", "partial"} and (source.counts or {}).get("observed", 0) > 0 and observed and verified:
        incident.status = proposal.status = "recovered"
    else:
        incident.status = proposal.status = "failed"
        incident.details = {**incident.details, "code_recovery_error": source.error_code if source and source.error_code else "code_execution_unproven"}
        if source and source.error_code in REPAIR_CODES and registry.active_release_id == incident.release_id:
            try:
                activate_release(db, proposal.base_release_id, rollback=True, expected_active_release_id=incident.release_id,
                                 expected_generation=incident.details["published_generation"], code_client=client)
                incident.details = {**incident.details, "rolled_back_to": str(proposal.base_release_id)}
            except CollectionError as exc:
                incident.details = {**incident.details, "rollback_skipped": exc.code}
    return incident


def reconcile_code_recoveries(db):
    """Settle terminal recovery tasks even when cancellation preceded worker claim."""
    identifiers = db.scalars(select(AdapterRepairIncident.recovery_task_id).join(
        TaskRun, TaskRun.id == AdapterRepairIncident.recovery_task_id).where(
        AdapterRepairIncident.status.in_(["code_recovery_queued", "code_recovering"]),
        TaskRun.status.in_(["succeeded", "partial", "failed", "cancelled", "waiting_login", "waiting_verification"])
    ).limit(100)).all()
    for identifier in identifiers:
        # finish takes registry -> incident; never lock incident first in this scan.
        finish_code_recovery(db, identifier)
