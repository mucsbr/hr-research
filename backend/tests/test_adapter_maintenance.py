from copy import deepcopy
from datetime import timedelta
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from pydantic import SecretStr
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.adapter_maintenance import (
    active_manifest, activate_release, builtin_manifest, enqueue_due_checks, ensure_registry,
    execute_check, fingerprint, generate_candidates, new_candidate, queue_check, register_all,
    shadow_validation, use_manifest, validate_manifest, validate_release,
)
from app.collection import CollectionError, CollectionResult
from app.config import get_settings
from app.db import get_db, session_factory
from app.llm import ModelGateway
from app.maintenance_api import router
from app.maintenance_models import AdapterMaintenanceRun, AdapterRegistry, AdapterRelease
from app.models import Job, ModelCall, TaskRun, User, now
from app.platform_collectors import collect_platform, contract
from app.security import current_user


@pytest.fixture
def manager(db):
    user = User(username="maintenance-test", password_hash="unused", role="admin")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client


def candidate_payload(kind="boss", version="fixture-0.2"):
    payload = deepcopy(builtin_manifest(kind))
    payload["version"] = version
    return payload


def test_builtin_registration_is_pinned_idempotent_and_off_by_default(db):
    registries = register_all(db)
    db.commit()
    register_all(db)
    db.commit()
    assert len(registries) == 5
    assert db.scalar(select(func.count()).select_from(AdapterRelease)) == 5
    assert all(not row.check_enabled and not row.agent_enabled for row in registries)
    assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0


@pytest.mark.parametrize("change", [
    {"command": "python unwanted.py"},
    {"detail_selectors": ["body"]},
    {"detail_selectors": [".job-sec-text, body"]},
    {"response_path_pattern": "(a+)+$"},
    {"list_paths": [["x"] * 12]},
    {"kind": "unregistered"},
])
def test_candidate_manifest_rejects_code_broad_selectors_and_complex_paths(change):
    with pytest.raises(CollectionError) as exc:
        validate_manifest({**candidate_payload(), **change}, "boss")
    assert exc.value.code in {"invalid_manifest", "adapter_kind_mismatch"}


def test_shadow_contract_does_not_write_jobs_tasks_or_model_calls(db):
    report = shadow_validation(candidate_payload())
    assert report["passed"] and all(item["passed"] for item in report["checks"])
    for model in (Job, TaskRun, ModelCall):
        assert db.scalar(select(func.count()).select_from(model)) == 0


def test_publish_and_rollback_switch_active_release_atomically(db):
    registry = ensure_registry(db, "boss")
    original_id = registry.active_release_id
    candidate = new_candidate(db, "boss", candidate_payload())
    db.commit()
    with pytest.raises(CollectionError) as exc:
        activate_release(db, candidate.id)
    assert exc.value.code == "release_not_validated"
    validate_release(db, candidate.id)
    activate_release(db, candidate.id)
    db.commit()
    assert active_manifest("boss")["version"] == "fixture-0.2"
    assert db.get(AdapterRelease, original_id).status == "retired"
    activate_release(db, original_id, rollback=True)
    db.commit()
    assert active_manifest("boss")["version"] == builtin_manifest("boss")["version"]
    assert db.get(AdapterRelease, candidate.id).status == "retired"


def test_wrong_field_mapping_fails_shadow_and_leaves_active_release(db):
    registry = ensure_registry(db, "boss")
    original_id = registry.active_release_id
    payload = candidate_payload()
    payload["fields"]["title"] = [["brandName"]]
    candidate = new_candidate(db, "boss", payload)
    validate_release(db, candidate.id)
    db.commit()
    assert candidate.status == "failed" and registry.active_release_id == original_id
    assert db.scalar(select(AdapterMaintenanceRun).where(AdapterMaintenanceRun.release_id == candidate.id)).status == "failed"


def test_invalid_json_candidate_failure_is_durable_via_api(manager, db):
    response = manager.post("/api/v1/adapter-maintenance/adapters/boss/candidates", json={
        "manifest": {**candidate_payload(), "execute": "not allowed"}})
    assert response.status_code == 409
    run = db.scalar(select(AdapterMaintenanceRun))
    assert run.status == "failed" and run.error_code == "invalid_manifest"


def test_validation_fingerprint_cannot_be_reused_after_content_mutation(db):
    candidate = new_candidate(db, "boss", candidate_payload())
    validate_release(db, candidate.id)
    candidate.manifest = {**candidate.manifest, "version": "modified-after-validation"}
    db.commit()
    with pytest.raises(CollectionError) as exc:
        activate_release(db, candidate.id)
    assert exc.value.code == "release_validation_stale"


def test_stale_candidate_cannot_replace_a_newer_release(db):
    first = new_candidate(db, "boss", candidate_payload(version="first"))
    second = new_candidate(db, "boss", candidate_payload(version="second"))
    validate_release(db, first.id)
    validate_release(db, second.id)
    activate_release(db, first.id)
    db.commit()
    with pytest.raises(CollectionError) as exc:
        activate_release(db, second.id)
    assert exc.value.code == "release_conflict"


def test_database_failure_never_silently_falls_back(monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", SecretStr("postgresql://configured-but-unavailable"))
    def unavailable():
        raise SQLAlchemyError("offline")
    monkeypatch.setattr("app.adapter_maintenance.session_factory", unavailable)
    with pytest.raises(CollectionError) as exc:
        active_manifest("boss")
    assert exc.value.code == "adapter_registry_unavailable"


def test_no_database_configuration_and_explicit_override_are_supported(monkeypatch):
    monkeypatch.setattr(get_settings(), "database_url", SecretStr(""))
    assert active_manifest("boss") == builtin_manifest("boss")
    with use_manifest("boss", candidate_payload()):
        assert contract("boss")["version"] == "fixture-0.2"
    assert contract("boss")["version"] == builtin_manifest("boss")["version"]


def test_one_collection_freezes_contract_across_midrun_publish(db, monkeypatch):
    ensure_registry(db, "boss")
    db.commit()
    before = active_manifest("boss")
    def run(*args, **kwargs):
        assert contract("boss") == before
        candidate = new_candidate(db, "boss", candidate_payload())
        validate_release(db, candidate.id)
        activate_release(db, candidate.id)
        db.commit()
        assert contract("boss") == before
        return CollectionResult()
    monkeypatch.setattr("app.platform_collectors._collect_platform", run)
    result = collect_platform(None, "boss", {}, {}, {})
    assert result.adapter_fingerprint == fingerprint(before)
    assert active_manifest("boss")["version"] == "fixture-0.2"


def test_daily_checks_are_off_until_enabled_and_enqueue_without_network(db, monkeypatch):
    register_all(db)
    assert enqueue_due_checks(db) == []
    registry = db.scalar(select(AdapterRegistry).where(AdapterRegistry.kind == "boss"))
    registry.check_enabled, registry.next_check_at = True, now() - timedelta(seconds=1)
    ids = enqueue_due_checks(db)
    db.commit()
    assert len(ids) == 1
    assert enqueue_due_checks(db) == []
    assert db.scalar(select(AdapterMaintenanceRun)).details["trigger"] == "daily"


def test_upstream_code_change_requires_engineering_and_keeps_active(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    active_id = registry.active_release_id
    run = queue_check(db, "boss")
    db.commit()
    requests = []
    class Fetcher:
        def fetch(self, url):
            requests.append(url)
            return url, json.dumps([{"sha": "a" * 40}])
        def close(self):
            pass
    monkeypatch.setattr("app.adapter_maintenance.PublicFetcher", Fetcher)
    execute_check(run.id)
    db.refresh(run)
    db.refresh(registry)
    assert run.status == "needs_engineering"
    assert registry.active_release_id == active_id
    assert len(requests) == 1 and requests[0].startswith("https://api.github.com/repos/eatmoreduck/boss-zhipin-scraper/")
    assert registry.upstream_commit != "a" * 40


def test_unconfigured_upstream_never_searches_or_executes_unknown_repository(db, monkeypatch):
    run = queue_check(db, "51job")
    db.commit()
    monkeypatch.setattr("app.adapter_maintenance.PublicFetcher", lambda: pytest.fail("must not fetch"))
    execute_check(run.id)
    db.refresh(run)
    assert run.status == "not_configured"


def test_agent_default_disabled_and_mock_transport_max_two_candidates(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    db.commit()
    with pytest.raises(CollectionError) as exc:
        generate_candidates(db, "boss", "布局变化")
    assert exc.value.code == "maintenance_agent_disabled"
    registry.agent_enabled = True
    db.commit()
    calls = []
    def response(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "not valid json"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 50, "completion_tokens": 10}})
    monkeypatch.setattr("app.adapter_maintenance.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    result = generate_candidates(db, "boss", "布局变化", max_candidates=2)
    assert result["status"] == "failed" and result["published"] is False
    assert len(calls) == 2 and db.scalar(select(func.count()).select_from(ModelCall)) == 2


def test_agent_valid_json_is_candidate_only_and_does_not_publish(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    original = registry.active_release_id
    db.commit()
    def response(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate_payload())}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 50, "completion_tokens": 300}})
    monkeypatch.setattr("app.adapter_maintenance.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    result = generate_candidates(db, "boss", "保留旧契约增加新路径", max_candidates=2)
    db.refresh(registry)
    assert result["status"] == "succeeded" and len(result["candidate_ids"]) == 1
    assert registry.active_release_id == original
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 1


def test_explicit_agent_auto_publish_is_a_bounded_generate_validate_publish_loop(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    original = registry.active_release_id
    db.commit()
    def response(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate_payload())}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 50, "completion_tokens": 300}})
    monkeypatch.setattr("app.adapter_maintenance.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    result = generate_candidates(db, "boss", "添加兼容字段", max_candidates=2, auto_publish=True)
    db.refresh(registry)
    assert result["published"] is True and result["status"] == "succeeded"
    assert str(registry.active_release_id) == result["published_release_id"]
    assert registry.active_release_id != original and db.get(AdapterRelease, original).status == "retired"
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 1


def test_explicit_auto_publish_failure_retains_current_release(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    original = registry.active_release_id
    db.commit()
    bad = candidate_payload()
    bad["fields"]["title"] = [["brandName"]]
    def response(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(bad)}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 50, "completion_tokens": 300}})
    monkeypatch.setattr("app.adapter_maintenance.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    result = generate_candidates(db, "boss", "添加兼容字段", max_candidates=1, auto_publish=True)
    db.refresh(registry)
    assert result["published"] is False and result["status"] == "failed"
    assert registry.active_release_id == original


def test_active_release_cannot_point_to_a_different_registry(db):
    boss = ensure_registry(db, "boss")
    liepin = ensure_registry(db, "liepin")
    original_id = boss.active_release_id
    boss.active_release_id = liepin.active_release_id
    db.commit()
    try:
        with pytest.raises(CollectionError) as exc:
            active_manifest("boss")
        assert exc.value.code == "adapter_release_unavailable"
    finally:
        boss.active_release_id = original_id
        db.commit()


@pytest.mark.parametrize("mode", ["cancel", "stopped"])
def test_cancel_during_generation_prevents_candidate_publication_and_has_recovery_clock(db, monkeypatch, mode):
    registry = ensure_registry(db, "boss")
    registry.agent_enabled = True
    original_id = registry.active_release_id
    db.commit()
    def response(request):
        with session_factory()() as cancel_db:
            task = cancel_db.scalar(select(TaskRun).where(TaskRun.kind == "adapter_json_repair"))
            assert task.started_at is not None and task.heartbeat_at is not None and task.attempt == 1
            if mode == "cancel":
                task.cancel_requested = True
            else:
                task.status, task.error_code = "failed", "worker_interrupted"
            cancel_db.commit()
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(candidate_payload())}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 50, "completion_tokens": 300}})
    monkeypatch.setattr("app.adapter_maintenance.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    result = generate_candidates(db, "boss", "页面变化", max_candidates=2, auto_publish=True)
    db.refresh(registry)
    assert result["status"] == ("cancelled" if mode == "cancel" else "failed")
    assert result["published"] is False
    assert registry.active_release_id == original_id
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 1
    assert result["candidate_ids"] == []
