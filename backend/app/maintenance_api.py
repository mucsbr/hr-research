from datetime import timedelta
import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field
from sqlalchemy import select

from app.adapter_maintenance import (
    FIXED_UPSTREAMS, KINDS, activate_release, builtin_manifest, ensure_registry, fingerprint,
    generate_candidates, new_candidate, queue_check, register_all, validate_release,
)
from app.collection import CollectionError
from app.adapter_code_models import AdapterCodeProposal
from app.adapter_runner_client import AdapterRunnerClient
from app.auto_maintenance_models import AdapterRepairIncident
from app.maintenance_models import AdapterMaintenanceRun, AdapterRegistry, AdapterRelease
from app.models import AuditEvent, TaskRun, now
from app.schemas import Input
from app.security import AdminUser, Db
from app.serialization import row

router = APIRouter(prefix="/api/v1/adapter-maintenance")


def known_kind(kind):
    if kind not in KINDS:
        raise HTTPException(404, "适配器未登记")


def failure(exc):
    return HTTPException(409, {"code": exc.code, "message": exc.message})


class MaintenanceSettings(Input):
    check_enabled: bool | None = None
    agent_enabled: bool | None = None
    auto_repair_enabled: bool | None = None
    auto_publish_enabled: bool | None = None
    code_agent_enabled: bool | None = None
    code_auto_publish_enabled: bool | None = None
    code_upstream_enabled: bool | None = None


class CandidateInput(Input):
    manifest: dict


class GenerateInput(Input):
    failure_description: str = Field(min_length=1, max_length=1500)
    max_candidates: int = Field(default=1, ge=1, le=2)
    auto_publish: bool = False


class CodePublicationInput(Input):
    expected_active_release_id: uuid.UUID
    expected_generation: int = Field(ge=0)


class VersionSwitchInput(Input):
    expected_active_release_id: uuid.UUID | None = None
    expected_generation: int | None = Field(default=None, ge=0)


@router.get("/adapters")
def adapters(db: Db, user: AdminUser):
    registered = {item.kind: item for item in db.scalars(select(AdapterRegistry))}
    result = []
    for kind in sorted(KINDS):
        registry = registered.get(kind)
        active = db.get(AdapterRelease, registry.active_release_id) if registry and registry.active_release_id else None
        builtin = builtin_manifest(kind)
        result.append({"kind": kind, "registered": bool(registry), "registry": row(registry) if registry else None,
                       "active": row(active) if active else {"version": builtin["version"], "fingerprint": fingerprint(builtin),
                                                            "manifest": builtin, "origin": "builtin", "status": "builtin_default"},
                       "upstream_available": kind in FIXED_UPSTREAMS})
    return result


@router.post("/register")
def register(db: Db, user: AdminUser):
    items = register_all(db)
    db.add(AuditEvent(user_id=user.id, action="adapters.registered", target="builtin",
                      detail={"kinds": sorted(KINDS)}))
    db.commit()
    return [row(item) for item in items]


@router.patch("/adapters/{kind}/settings")
def settings(kind: str, data: MaintenanceSettings, db: Db, user: AdminUser):
    known_kind(kind)
    if data.check_enabled and kind not in FIXED_UPSTREAMS:
        raise HTTPException(422, "此适配器尚未登记固定GitHub上游，不能启用定时检查")
    if data.code_upstream_enabled and kind not in FIXED_UPSTREAMS:
        raise HTTPException(422, "此适配器没有登记固定代码上游")
    registry = ensure_registry(db, kind)
    values = data.model_dump(exclude_unset=True)
    if any(value is None for value in values.values()):
        raise HTTPException(422, "开关不能为 null")
    for key, value in values.items():
        setattr(registry, key, value)
    if values.get("agent_enabled") is False:
        registry.auto_repair_enabled = registry.auto_publish_enabled = False
    elif registry.auto_repair_enabled and not registry.agent_enabled:
        db.rollback()
        raise HTTPException(422, "启用自动修复前必须显式启用维护模型")
    if values.get("code_agent_enabled") is False:
        registry.code_auto_publish_enabled = registry.code_upstream_enabled = False
    elif (registry.code_auto_publish_enabled or registry.code_upstream_enabled) and not registry.code_agent_enabled:
        db.rollback()
        raise HTTPException(422, "代码自动发布或上游升级需要独立启用代码维护模型")
    if {"check_enabled", "code_upstream_enabled", "code_agent_enabled"} & values.keys():
        registry.next_check_at = now() + timedelta(days=1) if (registry.check_enabled or registry.code_upstream_enabled) else None
    db.add(AuditEvent(user_id=user.id, action="adapter.settings_updated", target=kind, detail=values))
    db.commit()
    return row(registry)


@router.post("/adapters/{kind}/check", status_code=202)
def check(kind: str, db: Db, user: AdminUser):
    known_kind(kind)
    run = queue_check(db, kind)
    db.add(AuditEvent(user_id=user.id, action="adapter.upstream_check_requested", target=kind))
    db.commit()
    return row(run)


@router.get("/adapters/{kind}/releases")
def releases(kind: str, db: Db, user: AdminUser):
    known_kind(kind)
    registry = db.scalar(select(AdapterRegistry).where(AdapterRegistry.kind == kind))
    if not registry:
        return []
    return [row(item) for item in db.scalars(select(AdapterRelease).where(AdapterRelease.registry_id == registry.id)
                                            .order_by(AdapterRelease.created_at.desc()).limit(100))]


@router.post("/adapters/{kind}/candidates", status_code=201)
def candidate(kind: str, data: CandidateInput, db: Db, user: AdminUser):
    known_kind(kind)
    try:
        release = new_candidate(db, kind, data.manifest, user.id)
        db.commit()
        return row(release)
    except CollectionError as exc:
        db.commit()  # Validation failure run is intentionally durable; active release is unchanged.
        raise failure(exc) from None


@router.post("/releases/{release_id}/validate")
def validate(release_id: uuid.UUID, db: Db, user: AdminUser):
    try:
        release = validate_release(db, release_id)
        db.commit()
        return row(release)
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.post("/releases/{release_id}/publish")
def publish(release_id: uuid.UUID, db: Db, user: AdminUser):
    try:
        incident = db.scalar(select(AdapterRepairIncident).where(AdapterRepairIncident.release_id == release_id,
                                                                 AdapterRepairIncident.status == "awaiting_publish"))
        if incident:
            from app.auto_maintenance import publish_and_recover
            recovery = publish_and_recover(db, incident.id, user.id)
            db.commit()
            return {**row(db.get(AdapterRelease, release_id)), "recovery_task_id": str(recovery.id)}
        release = activate_release(db, release_id, user.id)
        db.commit()
        return row(release)
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.post("/releases/{release_id}/rollback")
def rollback(release_id: uuid.UUID, db: Db, user: AdminUser, data: VersionSwitchInput | None = None):
    try:
        release = db.get(AdapterRelease, release_id)
        if not release:
            raise HTTPException(404, "适配版本不存在")
        registry = db.get(AdapterRegistry, release.registry_id)
        active = db.get(AdapterRelease, registry.active_release_id)
        if ((release.manifest.get("program") or (active and active.manifest.get("program")))
                and (not data or data.expected_active_release_id is None or data.expected_generation is None)):
            raise HTTPException(422, "复合版本回滚必须提供页面读取到的活动版本与发布代次")
        release = activate_release(db, release_id, user.id, rollback=True,
            expected_active_release_id=data.expected_active_release_id if data else None,
            expected_generation=data.expected_generation if data else None)
        db.commit()
        return row(release)
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.get("/code-proposals")
def code_proposals(db: Db, user: AdminUser, kind: str | None = None,
                   limit: int = Query(50, ge=1, le=100)):
    query = select(AdapterCodeProposal, AdapterRegistry).join(AdapterRegistry, AdapterCodeProposal.registry_id == AdapterRegistry.id)
    if kind:
        known_kind(kind)
        query = query.where(AdapterRegistry.kind == kind)
    records = db.execute(query.order_by(AdapterCodeProposal.created_at.desc()).limit(limit)).all()
    result = []
    for proposal, registry in records:
        item = row(proposal, exclude={"source_snapshot", "evidence_cases", "base_spec", "draft_spec", "lock",
                                     "candidate_history", "validation_run_refs"})
        item.update({"kind": registry.kind, "current_active_release_id": str(registry.active_release_id),
                     "current_generation": registry.release_generation,
                     "validation_count": len(proposal.validation_run_refs),
                     "completed_validation_count": sum(ref.get("status") == "succeeded" for ref in proposal.validation_run_refs)})
        result.append(item)
    return result


@router.get("/code-proposals/{proposal_id}")
def code_proposal(proposal_id: uuid.UUID, db: Db, user: AdminUser):
    from app.adapter_code_release import proposal_detail
    proposal = db.get(AdapterCodeProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "代码维护提案不存在")
    return proposal_detail(db, proposal)


@router.post("/code-proposals/{proposal_id}/cancel")
def cancel_code_proposal(proposal_id: uuid.UUID, db: Db, user: AdminUser):
    from app.adapter_code_service import cancel_code_proposal as cancel_service
    try:
        result = cancel_service(proposal_id, client=AdapterRunnerClient())
        db.add(AuditEvent(user_id=user.id, action="adapter.code_proposal_cancelled", target=str(proposal_id)))
        db.commit()
        return result
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.post("/code-proposals/{proposal_id}/publish")
def publish_code_proposal(proposal_id: uuid.UUID, data: CodePublicationInput, db: Db, user: AdminUser):
    from app.adapter_code_release import publish_code_proposal as publish_service
    try:
        release = publish_service(db, proposal_id, user.id,
            expected_active_release_id=data.expected_active_release_id, expected_generation=data.expected_generation)
        db.commit()
        return row(release)
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.post("/adapters/{kind}/generate")
def generate(kind: str, data: GenerateInput, db: Db, user: AdminUser):
    known_kind(kind)
    try:
        return generate_candidates(db, kind, data.failure_description, user.id, data.max_candidates, data.auto_publish)
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.get("/runs")
def runs(db: Db, user: AdminUser, limit: int = Query(50, ge=1, le=100)):
    return [row(item) for item in db.scalars(select(AdapterMaintenanceRun)
                                            .order_by(AdapterMaintenanceRun.created_at.desc()).limit(limit))]


@router.get("/runs/{run_id}")
def run(run_id: uuid.UUID, db: Db, user: AdminUser):
    item = db.get(AdapterMaintenanceRun, run_id)
    if not item:
        raise HTTPException(404, "维护记录不存在")
    return row(item)


@router.get("/incidents")
def repair_incidents(db: Db, user: AdminUser, limit: int = Query(50, ge=1, le=100)):
    result = []
    for incident in db.scalars(select(AdapterRepairIncident).order_by(AdapterRepairIncident.created_at.desc()).limit(limit)):
        registry = db.get(AdapterRegistry, incident.registry_id)
        result.append({**row(incident, exclude={"evidence"}), "kind": registry.kind,
                       "code_agent_enabled": registry.code_agent_enabled})
    return result


@router.post("/incidents/{incident_id}/code-proposal", status_code=202)
def incident_code_proposal(incident_id: uuid.UUID, db: Db, user: AdminUser):
    from app.adapter_code_orchestration import queue_code_from_failure
    try:
        proposal = queue_code_from_failure(db, incident_id)
        if proposal is None:
            raise CollectionError("code_evidence_unavailable", "该事件没有可用代码维护证据或独立开关未开启；未调用模型")
        db.add(AuditEvent(user_id=user.id, action="adapter.code_failure_queued", target=str(proposal.id),
                          detail={"incident_id": str(incident_id)}))
        db.commit()
        return row(proposal, exclude={"base_spec", "draft_spec", "evidence_cases", "source_snapshot"})
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None


@router.get("/incidents/{incident_id}")
def repair_incident(incident_id: uuid.UUID, db: Db, user: AdminUser):
    item = db.get(AdapterRepairIncident, incident_id)
    if not item:
        raise HTTPException(404, "自动维护事件不存在")
    return row(item)


@router.post("/incidents/{incident_id}/cancel")
def cancel_incident(incident_id: uuid.UUID, db: Db, user: AdminUser):
    incident = db.scalar(select(AdapterRepairIncident).where(AdapterRepairIncident.id == incident_id).with_for_update())
    if not incident:
        raise HTTPException(404, "自动维护事件不存在")
    incident.cancel_requested = True
    for task_id in (incident.maintenance_task_id, incident.recovery_task_id):
        task = db.get(TaskRun, task_id) if task_id else None
        if task and task.status in {"queued", "running"}:
            task.cancel_requested = True
            if task.status == "queued":
                task.status, task.finished_at = "cancelled", now()
    if incident.status in {"queued", "awaiting_publish", "recovery_queued"}:
        incident.status = "cancelled"
    db.add(AuditEvent(user_id=user.id, action="adapter.auto_repair_cancelled", target=str(incident.id)))
    db.commit()
    return row(incident, exclude={"evidence"})


@router.post("/incidents/{incident_id}/publish")
def publish_incident(incident_id: uuid.UUID, db: Db, user: AdminUser):
    from app.auto_maintenance import publish_and_recover
    try:
        task = publish_and_recover(db, incident_id, user.id)
        db.commit()
        return {"incident_id": str(incident_id), "recovery_task_id": str(task.id)}
    except CollectionError as exc:
        db.rollback()
        raise failure(exc) from None
