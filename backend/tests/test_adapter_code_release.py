from copy import deepcopy
import hashlib
import hmac
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.adapter_code_models import AdapterCodePackage
from app.adapter_code_package import canonical_bytes, content_sha256
from app.adapter_code_release import create_release_from_proposal, publish_code_proposal
from app.adapter_code_service import advance_code_validation, cancel_code_proposal, create_code_proposal, generate_code_candidate
from app.adapter_maintenance import (
    activate_release, builtin_manifest, ensure_registry, fingerprint, generate_candidates,
    new_candidate, shadow_validation, validate_release,
)
from app.collection import CollectionError
from app.config import get_settings
from app.maintenance_models import AdapterRelease
from app.models import ModelCall, User
import test_adapter_code_service as support
import test_adapter_maintenance as maintenance_support

manager = maintenance_support.manager


class ReleaseRunner(support.SignedOutcomeRunner):
    def __init__(self):
        super().__init__("succeeded", None)

    def get_run(self, run_id):
        result = super().get_run(run_id)
        request = super(support.SignedOutcomeRunner, self).get_run(run_id)["request"]
        output = next(case["expected"] for case in support.cases() if case["operation"] == request["operation"])
        result["output"] = output
        receipt = {key: value for key, value in result["receipt"].items() if key != "signature"}
        receipt.update(output=output, output_sha256=content_sha256(output), exit_code=0)
        key = hmac.new(self.token.encode(), b"hr-adapter-runner-receipt-v1", hashlib.sha256).digest()
        receipt["signature"] = hmac.new(key, canonical_bytes(receipt), hashlib.sha256).hexdigest()
        result["receipt"] = receipt
        return result


def verified(db, **kwargs):
    proposal, registry = support.seed(db, **kwargs)
    registry.agent_enabled = False  # JSON permission is deliberately independent.
    db.commit()
    calls, runner = [], ReleaseRunner()
    generate_code_candidate(proposal.id, gateway=support.gateway(support.candidate(), calls), client=runner)
    for _ in range(3):
        result = advance_code_validation(proposal.id, client=runner)
    assert result["status"] == "validated"
    db.refresh(proposal)
    db.refresh(registry)
    return proposal, registry, runner, calls


def test_composite_single_pointer_publish_and_historical_rollback_after_generation_changes(db):
    proposal, registry, runner, calls = verified(db)
    base_id = registry.active_release_id
    release = publish_code_proposal(db, proposal.id, expected_active_release_id=base_id,
                                    expected_generation=0, client=runner)
    db.commit()
    assert registry.active_release_id == release.id and registry.release_generation == 1
    assert release.manifest["program"] == {
        "contract": "python_adapter_v1", "bundle_sha256": proposal.bundle_sha,
        "lock_sha256": content_sha256(proposal.lock), "runtime_sha256": "sha256:" + "a" * 64,
        "dependency_environment_sha256": "b" * 64}
    assert proposal.status == "published" and release.validation["code_published_once"]
    activate_release(db, base_id, rollback=True, expected_active_release_id=release.id, expected_generation=1)
    db.commit()
    registry.code_agent_enabled = False
    db.commit()
    activate_release(db, release.id, rollback=True, expected_active_release_id=base_id, expected_generation=2, code_client=runner)
    db.commit()
    assert registry.active_release_id == release.id and registry.release_generation == 3
    assert len(calls) == 1


def test_pending_code_release_rejects_aba_even_when_original_pointer_returns(db):
    proposal, registry, runner, _ = verified(db)
    base_id = registry.active_release_id
    code_release = create_release_from_proposal(db, proposal.id, client=runner)
    db.commit()
    other = new_candidate(db, "boss", {**builtin_manifest("boss"), "version": "intervening"})
    validate_release(db, other.id)
    activate_release(db, other.id)
    db.commit()
    activate_release(db, base_id, rollback=True)
    db.commit()
    with pytest.raises(CollectionError) as error:
        activate_release(db, code_release.id, expected_active_release_id=base_id, expected_generation=2, code_client=runner)
    assert error.value.code == "release_conflict"
    db.rollback()
    db.refresh(registry)
    assert registry.active_release_id == base_id and registry.release_generation == 2


def test_cancelled_proposal_cannot_publish_precreated_release(db):
    proposal, registry, runner, _ = verified(db)
    base_id = registry.active_release_id
    release = create_release_from_proposal(db, proposal.id, client=runner)
    db.commit()
    cancel_code_proposal(proposal.id, client=runner)
    with pytest.raises(CollectionError) as error:
        activate_release(db, release.id, code_client=runner)
    assert error.value.code == "cancelled"
    db.rollback()
    db.refresh(registry)
    assert registry.active_release_id == base_id


def test_json_candidate_and_forged_validation_cannot_bypass_code_proofs(db):
    proposal, registry, runner, _ = verified(db)
    release = create_release_from_proposal(db, proposal.id, client=runner)
    db.commit()
    with pytest.raises(CollectionError) as error:
        new_candidate(db, "boss", deepcopy(release.manifest))
    assert error.value.code == "code_candidate_path_required"
    db.rollback()
    forged = AdapterRelease(registry_id=registry.id, version="forged", manifest=release.manifest,
                            fingerprint=fingerprint(release.manifest), status="validated", origin="manual",
                            replaces_release_id=registry.active_release_id, replaces_generation=registry.release_generation,
                            validation={"passed": True, "fingerprint": fingerprint(release.manifest)})
    db.add(forged)
    db.commit()
    with pytest.raises(CollectionError) as error:
        activate_release(db, forged.id, code_client=runner)
    assert error.value.code == "code_validation_missing"


def test_active_code_cannot_be_stripped_by_json_candidate_or_paid_json_generation(db):
    proposal, registry, runner, _ = verified(db)
    release = publish_code_proposal(db, proposal.id, expected_active_release_id=registry.active_release_id,
                                    expected_generation=registry.release_generation, client=runner)
    registry.agent_enabled = True
    db.commit()
    before = db.scalar(select(func.count()).select_from(ModelCall))
    with pytest.raises(CollectionError) as error:
        new_candidate(db, "boss", builtin_manifest("boss"))
    assert error.value.code == "code_candidate_path_required"
    db.rollback()
    with pytest.raises(CollectionError) as error:
        generate_candidates(db, "boss", "不得绕行旧JSON生成")
    assert error.value.code == "code_candidate_path_required"
    assert db.scalar(select(func.count()).select_from(ModelCall)) == before
    assert registry.active_release_id == release.id


def test_publish_rechecks_local_complete_bundle(db):
    proposal, registry, runner, _ = verified(db)
    release = create_release_from_proposal(db, proposal.id, client=runner)
    db.commit()
    package = db.scalar(select(AdapterCodePackage).where(AdapterCodePackage.bundle_sha == proposal.bundle_sha))
    (Path(get_settings().storage_dir) / package.file_ref).write_text("{}")
    with pytest.raises(CollectionError) as error:
        activate_release(db, release.id, code_client=runner)
    assert error.value.code in {"code_package_corrupt", "code_package_unavailable"}
    assert registry.active_release_id == proposal.base_release_id


def test_publish_rechecks_signed_outputs_and_frozen_oracle(db):
    proposal, registry, runner, _ = verified(db)
    release = create_release_from_proposal(db, proposal.id, client=runner)
    db.commit()
    refs = deepcopy(proposal.validation_run_refs)
    refs[0]["receipt"]["output"] = {"forged": True}
    proposal.validation_run_refs = refs
    db.commit()
    with pytest.raises(CollectionError) as error:
        activate_release(db, release.id, code_client=runner)
    assert error.value.code == "runner_receipt_mismatch"
    assert registry.active_release_id == proposal.base_release_id


def test_code_permission_is_not_implied_by_old_json_permission(db):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    db.commit()
    with pytest.raises(CollectionError) as error:
        create_code_proposal(db, "boss", base_spec=support.spec(), source_snapshot={}, evidence_cases=support.cases())
    assert error.value.code == "maintenance_agent_disabled"
    registry.agent_enabled, registry.code_agent_enabled = False, True
    db.commit()
    assert create_code_proposal(db, "boss", base_spec=support.spec(), source_snapshot={}, evidence_cases=support.cases())


def test_official_native_shadow_checks_real_jsonld_fields_without_model(db):
    report = shadow_validation(builtin_manifest("official"))
    assert report["passed"] and all(item["passed"] for item in report["checks"])
    assert {item["name"] for item in report["checks"]} >= {"native_jsonld_fields", "unknown_document_empty"}
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


def test_management_api_is_admin_only_and_rejects_browser_oracles(manager, db, monkeypatch):
    proposal, registry, runner, _ = verified(db)
    monkeypatch.setattr("app.adapter_code_release.AdapterRunnerClient", lambda: runner)
    listing = manager.get("/api/v1/adapter-maintenance/code-proposals?kind=boss")
    assert listing.status_code == 200 and listing.json()[0]["id"] == str(proposal.id)
    assert "evidence_cases" not in listing.json()[0]
    detail = manager.get("/api/v1/adapter-maintenance/code-proposals/" + str(proposal.id))
    assert detail.status_code == 200 and detail.json()["source_diff"]
    path = "/api/v1/adapter-maintenance/code-proposals/" + str(proposal.id) + "/publish"
    values = {"expected_active_release_id": str(registry.active_release_id), "expected_generation": registry.release_generation}
    assert manager.post(path, json={**values, "oracle": {"passed": True}}).status_code == 422
    assert manager.post(path, json=values).status_code == 200
    db.refresh(registry)
    assert registry.release_generation == 1
    original = manager.get("/api/v1/adapter-maintenance/adapters/boss/releases").json()
    base = next(item for item in original if item["id"] == str(proposal.base_release_id))
    assert manager.post("/api/v1/adapter-maintenance/releases/" + base["id"] + "/rollback", json={}).status_code == 422
    admin = db.scalar(select(User).where(User.username == "maintenance-test"))
    admin.role = "user"
    db.commit()
    assert manager.get("/api/v1/adapter-maintenance/code-proposals").status_code == 403
    assert manager.post(path, json=values).status_code == 403


def test_code_settings_do_not_inherit_json_agent_or_auto_publish(manager, db):
    registry = ensure_registry(db, "boss")
    db.commit()
    path = "/api/v1/adapter-maintenance/adapters/boss/settings"
    assert manager.patch(path, json={"agent_enabled": True, "auto_repair_enabled": True,
                                     "auto_publish_enabled": True}).status_code == 200
    db.refresh(registry)
    assert not registry.code_agent_enabled and not registry.code_auto_publish_enabled and not registry.code_upstream_enabled
    assert manager.patch(path, json={"code_auto_publish_enabled": True}).status_code == 422
    assert manager.patch(path, json={"code_agent_enabled": True, "code_auto_publish_enabled": True}).status_code == 200
    assert manager.patch(path, json={"agent_enabled": False}).status_code == 200
    db.refresh(registry)
    assert registry.code_agent_enabled and registry.code_auto_publish_enabled
    assert manager.patch(path, json={"code_agent_enabled": False}).status_code == 200
    db.refresh(registry)
    assert not registry.code_agent_enabled and not registry.code_auto_publish_enabled and not registry.code_upstream_enabled


def test_manual_fixed_upstream_publish_advances_commit_in_same_transaction(db):
    registry = ensure_registry(db, "boss")
    registry.upstream_commit = "a" * 40
    db.commit()
    source = {"repository": "https://github.com/eatmoreduck/boss-zhipin-scraper",
              "base_commit": "a" * 40, "target_commit": "b" * 40,
              "files": [{"path": "parser.py", "diff": "+repaired_parser"}]}
    proposal, registry, runner, _ = verified(db, origin="fixed_upstream", source_snapshot=source)
    publish_code_proposal(db, proposal.id, expected_active_release_id=registry.active_release_id,
                          expected_generation=registry.release_generation, client=runner)
    db.commit()
    assert registry.upstream_commit == "b" * 40


def test_upstream_commit_conflict_cannot_publish_even_if_release_pointer_is_unchanged(db):
    registry = ensure_registry(db, "boss")
    registry.upstream_commit = "a" * 40
    db.commit()
    source = {"repository": "https://github.com/eatmoreduck/boss-zhipin-scraper",
              "base_commit": "a" * 40, "target_commit": "b" * 40,
              "files": [{"path": "parser.py", "diff": "+repaired_parser"}]}
    proposal, registry, runner, _ = verified(db, origin="fixed_upstream", source_snapshot=source)
    previous = registry.active_release_id
    registry.upstream_commit = "c" * 40
    db.commit()
    with pytest.raises(CollectionError) as error:
        publish_code_proposal(db, proposal.id, expected_active_release_id=previous,
                              expected_generation=registry.release_generation, client=runner)
    assert error.value.code == "upstream_revision_conflict"
    db.rollback()
    db.refresh(registry)
    assert registry.active_release_id == previous and registry.upstream_commit == "c" * 40
