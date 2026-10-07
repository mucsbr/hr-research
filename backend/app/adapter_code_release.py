"""Composite adapter publication through the existing single release pointer."""
from copy import deepcopy
from datetime import datetime, timezone
import difflib
import re
import uuid

from sqlalchemy import select

from app.adapter_code_models import AdapterCodePackage, AdapterCodeProposal
from app.adapter_code_package import build_bundle, content_sha256, load_bundle
from app.adapter_code_service import check_proposal_active, validation_summary
from app.adapter_code_upstream import FIXED_CODE_UPSTREAMS
from app.adapter_runner_client import AdapterRunnerClient
from app.collection import CollectionError
from app.maintenance_models import AdapterMaintenanceRun, AdapterRegistry, AdapterRelease
from app.models import AuditEvent, TaskRun, now


def _proof(db, proposal, *, client):
    package = db.scalar(select(AdapterCodePackage).where(AdapterCodePackage.bundle_sha == proposal.bundle_sha))
    if not package or not proposal.bundle_sha:
        raise CollectionError("code_package_missing", "代码候选缺少不可变产物")
    bundle = load_bundle(package.file_ref, expected_sha256=proposal.bundle_sha)
    if (bundle != build_bundle(proposal.draft_spec, proposal.lock)
            or package.lock_sha != content_sha256(bundle["lock"])):
        raise CollectionError("code_package_corrupt", "完整源码或依赖锁与已验证候选不一致")
    task = db.get(TaskRun, proposal.task_id, populate_existing=True)
    runtime = task.config_snapshot.get("runner_runtime_sha256") if task else None
    if not runtime:
        raise CollectionError("code_runtime_missing", "代码候选缺少验证时固定的运行环境")
    environments = set()
    cases = {case["id"]: case for case in proposal.evidence_cases}
    for ref in proposal.validation_run_refs:
        request = ref.get("request", {})
        case = cases.get(ref.get("case_id"))
        if (not case or request.get("bundle_sha256") != package.bundle_sha
                or request.get("operation") != case["operation"] or request.get("input") != case["input"]
                or request.get("idempotency_key") != ref.get("idempotency_key")
                or content_sha256(request) != ref.get("request_sha256")):
            raise CollectionError("code_validation_binding", "验证请求没有绑定当前不可变代码包")
        result = ref.get("receipt", {})
        output = client.verify_result(result, request=request, lock_sha256=package.lock_sha,
                                      expected_runtime=runtime)
        if content_sha256(output) != content_sha256(ref.get("output")) or result.get("run_id") != ref.get("run_id"):
            raise CollectionError("code_validation_binding", "保存的输出与签名回执不一致")
        environments.add(result["receipt"]["dependency_environment_sha256"])
    report = validation_summary(proposal.evidence_cases, proposal.validation_run_refs, bundle_sha=proposal.bundle_sha)
    if not report["passed"] or len(environments) != 1:
        raise CollectionError("code_validation_failed", "代码候选未通过所有可信预期或混用了不同依赖环境")
    program = {"contract": "python_adapter_v1", "bundle_sha256": package.bundle_sha,
               "lock_sha256": package.lock_sha, "runtime_sha256": "sha256:" + runtime.removeprefix("sha256:"),
               "dependency_environment_sha256": next(iter(environments))}
    return package, report, program


def create_release_from_proposal(db, proposal_id, actor_id=None, *, client=None):
    from app.adapter_maintenance import fingerprint, shadow_validation, validate_manifest

    proposal, registry, _ = check_proposal_active(db, proposal_id, statuses={"validated", "awaiting_publish"})
    client = client or AdapterRunnerClient()
    package, report, program = _proof(db, proposal, client=client)
    if package.kind != registry.kind:
        raise CollectionError("adapter_kind_mismatch", "代码包不能跨平台创建发布版本")
    if proposal.release_id:
        existing = db.scalar(select(AdapterRelease).where(AdapterRelease.id == proposal.release_id).with_for_update())
        if not existing:
            raise CollectionError("code_release_missing", "候选绑定的复合版本不存在")
        verify_code_release(db, existing, client=client)
        return existing
    baseline = db.get(AdapterRelease, proposal.base_release_id, populate_existing=True)
    if not baseline or baseline.registry_id != registry.id:
        raise CollectionError("release_conflict", "候选基线版本不存在或不属于当前平台")
    manifest = validate_manifest({**deepcopy(baseline.manifest), "program": program,
                                  "version": "code-" + package.bundle_sha[:16]}, registry.kind)
    regression = shadow_validation(manifest)
    if not regression["passed"]:
        raise CollectionError("contract_regression", "复合版本的JSON兼容契约未通过")
    release = AdapterRelease(registry_id=registry.id, version=manifest["version"], manifest=manifest,
                             fingerprint=fingerprint(manifest), status="validated", origin="code_proposal",
                             replaces_release_id=proposal.base_release_id,
                             replaces_generation=proposal.base_generation, created_by=actor_id,
                             validation={**regression, "code_proposal_id": str(proposal.id), "code_validation": report})
    db.add(release)
    db.flush()
    proposal.release_id, proposal.status = release.id, "awaiting_publish"
    db.add(AdapterMaintenanceRun(registry_id=registry.id, release_id=release.id, kind="code_composite_validation",
                                  status="succeeded", started_at=now(), finished_at=now(),
                                  details={"proposal_id": str(proposal.id), "bundle_sha256": package.bundle_sha,
                                           "base_generation": proposal.base_generation}))
    db.add(AuditEvent(user_id=actor_id, action="adapter.code_release_created", target=str(release.id),
                      detail={"proposal_id": str(proposal.id), "bundle_sha256": package.bundle_sha}))
    db.flush()
    return release


def verify_code_release(db, release, *, client=None, historical=False):
    """Recheck saved bytes and receipts. Historical rollback deliberately ignores old base CAS."""
    from app.adapter_maintenance import fingerprint, validate_manifest

    manifest = validate_manifest(release.manifest)
    program = manifest.get("program")
    if not program:
        raise CollectionError("code_reference_missing", "复合版本缺少代码引用")
    try:
        proposal_id = uuid.UUID(release.validation["code_proposal_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise CollectionError("code_validation_missing", "代码版本缺少可信提案关联") from exc
    if historical:
        proposal = db.scalar(select(AdapterCodeProposal).where(AdapterCodeProposal.id == proposal_id).with_for_update())
        if release.status not in {"published", "retired"} or not release.validation.get("code_published_once"):
            raise CollectionError("code_history_unpublished", "只能回滚到曾正式发布的代码版本")
    else:
        proposal, _, _ = check_proposal_active(db, proposal_id, statuses={"validated", "awaiting_publish"})
    if (not proposal or proposal.release_id != release.id or proposal.registry_id != release.registry_id
            or release.replaces_release_id != proposal.base_release_id
            or release.replaces_generation != proposal.base_generation):
        raise CollectionError("code_validation_binding", "复合版本与提案或原始验证基线不一致")
    if not historical and proposal.origin == "fixed_upstream":
        registry = db.get(AdapterRegistry, proposal.registry_id, populate_existing=True)
        source = proposal.source_snapshot
        repository = FIXED_CODE_UPSTREAMS.get(registry.kind)
        if (not repository or source.get("repository") != "https://github.com/" + repository
                or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("base_commit", "")))
                or not re.fullmatch(r"[0-9a-f]{40}", str(source.get("target_commit", "")))
                or source["base_commit"] == source["target_commit"]
                or registry.upstream_commit != source["base_commit"]):
            raise CollectionError("upstream_revision_conflict", "固定上游基线已变化或提交证据不正确，未发布代码")
    package, report, verified_program = _proof(db, proposal, client=client or AdapterRunnerClient())
    if (program != verified_program or package.kind != manifest["kind"]
            or not release.validation.get("passed") or release.validation.get("code_validation") != report
            or release.fingerprint != fingerprint(manifest)
            or release.validation.get("fingerprint") != release.fingerprint):
        raise CollectionError("code_validation_stale", "代码引用、完整包或验证报告发生变化，拒绝切换版本")
    return proposal


def publish_code_proposal(db, proposal_id, actor_id=None, *, expected_active_release_id,
                          expected_generation, client=None):
    from app.adapter_maintenance import activate_release

    release = create_release_from_proposal(db, proposal_id, actor_id, client=client)
    release = activate_release(db, release.id, actor_id, expected_active_release_id=expected_active_release_id,
                               expected_generation=expected_generation, code_client=client)
    return release


def mark_code_published(db, release, *, rollback=False, actor_id=None):
    identifier = uuid.UUID(release.validation["code_proposal_id"])
    proposal = db.get(AdapterCodeProposal, identifier)
    release.validation = {**release.validation, "code_published_once": True,
                          "code_last_activated_at": datetime.now(timezone.utc).isoformat()}
    if not rollback:
        proposal.status = "published"
        if proposal.origin == "fixed_upstream":
            # Shared by automatic and manual publication; never leave the observed baseline stale.
            registry = db.get(AdapterRegistry, proposal.registry_id)
            registry.upstream_commit = proposal.source_snapshot["target_commit"]
        if proposal.source_snapshot.get("automatic") and proposal.source_snapshot.get("incident_id"):
            from app.adapter_code_orchestration import on_code_published
            on_code_published(db, proposal, release, manual_actor_id=actor_id)
    return proposal


def proposal_detail(db, proposal):
    from app.serialization import row

    registry = db.get(AdapterRegistry, proposal.registry_id)
    result = row(proposal)
    paths = sorted(set(proposal.base_spec.get("files", {})) | set(proposal.draft_spec.get("files", {})))
    diffs = []
    remaining = 60000
    for path in paths:
        before = proposal.base_spec.get("files", {}).get(path, "")
        after = proposal.draft_spec.get("files", {}).get(path, "")
        if before == after or not proposal.draft_spec:
            continue
        diff = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                             fromfile="base/" + path, tofile="candidate/" + path))
        shown = diff[:remaining]
        remaining -= len(shown)
        diffs.append({"path": path, "diff": shown, "truncated": len(shown) != len(diff)})
    task = db.get(TaskRun, proposal.task_id)
    result.update({"kind": registry.kind, "current_active_release_id": str(registry.active_release_id),
                   "current_generation": registry.release_generation, "source_diff": diffs,
                   "dependency_diff": {"before": proposal.base_spec.get("dependencies", []),
                                       "after": proposal.draft_spec.get("dependencies", [])},
                   "task_summary": task.summary if task else {},
                   "model_contexts": task.config_snapshot.get("code_model_contexts", []) if task else []})
    return result
