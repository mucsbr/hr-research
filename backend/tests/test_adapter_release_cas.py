"""Two-session release races against real PostgreSQL, without network or models."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from app.adapter_maintenance import activate_release, ensure_registry, new_candidate, validate_release
from app.auto_maintenance import _queue_recovery, finish_recovery, prepare_recovery
from app.collection import CollectionError
from app.db import session_factory
from app.maintenance_models import AdapterRegistry
from app.models import Job, ModelCall, SourceRun, TaskRun
from test_adapter_maintenance import candidate_payload
from test_auto_maintenance import enqueue, seed


def candidate(db, version):
    release = new_candidate(db, "boss", candidate_payload(version=version))
    validate_release(db, release.id)
    db.commit()
    return release


def failed_recovery(db, state, incident, release):
    _, _, binding, _, _, _, _, registry = state
    incident.release_id = release.id
    recovery = _queue_recovery(db, incident, registry, release)
    recovery.status, recovery.attempt = "failed", 1
    db.add(SourceRun(task_id=recovery.id, binding_id=binding.id, status="failed",
                     error_code="source_contract_changed", scope_hash="cas-recovery"))
    incident.status = "recovering"
    db.commit()
    return recovery


@pytest.mark.parametrize("aba", [False, True], ids=["newer-release", "aba-release"])
def test_publish_rechecks_cached_registry_after_other_session_commits(db, aba):
    registry = ensure_registry(db, "boss")
    db.commit()
    original_id = registry.active_release_id
    late = candidate(db, "late-candidate")
    competitor = candidate(db, "competing-candidate")
    # Keep the old registry object in this Session's identity map. expire_on_commit
    # is false in the actual application, so a plain SELECT FOR UPDATE is insufficient.
    assert db.get(AdapterRegistry, registry.id) is registry
    assert registry.active_release_id == original_id
    barrier = Barrier(2)

    def publish_other():
        barrier.wait(timeout=5)
        with session_factory()() as other:
            activate_release(other, competitor.id)
            other.commit()
            if aba:
                activate_release(other, original_id, rollback=True)
                other.commit()
        barrier.wait(timeout=5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(publish_other)
        barrier.wait(timeout=5)
        barrier.wait(timeout=5)
        completed.result(timeout=5)
        with pytest.raises(CollectionError) as exc:
            activate_release(db, late.id)
        assert exc.value.code == "release_conflict"
    db.rollback()
    with session_factory()() as current:
        active_registry = current.get(AdapterRegistry, registry.id)
        assert active_registry.active_release_id == (original_id if aba else competitor.id)
        assert active_registry.release_generation == (2 if aba else 1)
        assert not current.scalars(select(ModelCall)).all()
        assert not current.scalars(select(Job)).all()


@pytest.mark.parametrize("aba", [False, True], ids=["newer-release", "aba-release"])
def test_automatic_rollback_cannot_replace_concurrent_release_or_aba(db, monkeypatch, aba):
    state = seed(db)
    _, _, _, _, _, _, _, registry = state
    original_id = registry.active_release_id
    incident = enqueue(db, state)
    broken = candidate(db, "failed-recovery-release")
    activate_release(db, broken.id)
    db.commit()
    recovery = failed_recovery(db, state, incident, broken)
    competitor = candidate(db, "newer-working-release")
    barrier = Barrier(2)

    def publish_other():
        barrier.wait(timeout=5)
        with session_factory()() as other:
            activate_release(other, competitor.id)
            other.commit()
            if aba:
                activate_release(other, broken.id, rollback=True)
                other.commit()
        barrier.wait(timeout=5)

    def pause_before_rollback(*args, **kwargs):
        # finish_recovery has observed the failed release but has not acquired the
        # registry lock yet. Commit another publisher exactly inside that window.
        barrier.wait(timeout=5)
        barrier.wait(timeout=5)
        return activate_release(*args, **kwargs)

    monkeypatch.setattr("app.auto_maintenance.activate_release", pause_before_rollback)
    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(publish_other)
        finish_recovery(db, recovery.id)
        db.commit()
        completed.result(timeout=5)
    with session_factory()() as current:
        active_registry = current.get(AdapterRegistry, registry.id)
        active = active_registry.active_release_id
        assert active == (broken.id if aba else competitor.id)
        assert active_registry.release_generation == (3 if aba else 2)
        assert active != original_id
        assert current.get(TaskRun, recovery.id).status == "failed"
        assert not current.scalars(select(ModelCall)).all()
        assert not current.scalars(select(Job)).all()
    db.refresh(incident)
    assert incident.status == "failed"
    assert incident.details["rollback_skipped"] == "release_conflict"


def test_automatic_rollback_rejects_aba_completed_before_finish_enters(db):
    state = seed(db)
    registry = state[-1]
    original_id = registry.active_release_id
    incident = enqueue(db, state)
    broken = candidate(db, "completed-aba-original-recovery")
    activate_release(db, broken.id)
    db.commit()
    recovery = failed_recovery(db, state, incident, broken)
    competitor = candidate(db, "completed-aba-new-publication")
    # Both publications finish before finish_recovery performs its first read.
    with session_factory()() as other:
        activate_release(other, competitor.id)
        other.commit()
        activate_release(other, broken.id, rollback=True)
        other.commit()
    finish_recovery(db, recovery.id)
    db.commit()
    with session_factory()() as current:
        active = current.get(AdapterRegistry, registry.id)
        assert active.active_release_id == broken.id and active.active_release_id != original_id
        assert active.release_generation == 3
    assert incident.status == "failed"
    assert incident.details["rollback_skipped"] == "release_conflict"


def test_recovery_snapshot_records_publication_generation_and_prepare_rejects_completed_aba(db):
    state = seed(db)
    registry = state[-1]
    incident = enqueue(db, state)
    repaired = candidate(db, "generation-bound-recovery")
    activate_release(db, repaired.id)
    db.commit()
    recovery = failed_recovery(db, state, incident, repaired)
    frozen = recovery.config_snapshot["sources_snapshot"][0]
    assert frozen["expected_release_id"] == str(repaired.id)
    assert frozen["expected_release_generation"] == incident.details["published_generation"] == 1
    incident.status, recovery.status = "recovery_queued", "running"
    db.commit()
    competitor = candidate(db, "generation-bound-competitor")
    with session_factory()() as other:
        activate_release(other, competitor.id)
        other.commit()
        activate_release(other, repaired.id, rollback=True)
        other.commit()
    with pytest.raises(CollectionError) as exc:
        prepare_recovery(db, recovery.id)
    assert exc.value.code == "adapter_version_conflict"
    db.rollback()
    assert db.get(AdapterRegistry, registry.id, populate_existing=True).release_generation == 3


@pytest.mark.parametrize("missing", ["incident", "snapshot", "mismatch"])
def test_historical_recovery_cannot_invent_missing_publication_generation(db, missing):
    state = seed(db)
    registry = state[-1]
    incident = enqueue(db, state)
    repaired = candidate(db, "historical-recovery")
    activate_release(db, repaired.id)
    db.commit()
    recovery = failed_recovery(db, state, incident, repaired)
    if missing == "incident":
        incident.details = {key: value for key, value in incident.details.items() if key != "published_generation"}
    else:
        frozen = dict(recovery.config_snapshot["sources_snapshot"][0])
        if missing == "snapshot":
            frozen.pop("expected_release_generation")
        else:
            frozen["expected_release_generation"] += 1
        recovery.config_snapshot = {**recovery.config_snapshot, "sources_snapshot": [frozen]}
    incident.status = "recovery_queued"
    db.commit()
    with pytest.raises(CollectionError) as exc:
        prepare_recovery(db, recovery.id)
    assert exc.value.code == "adapter_version_conflict"
    db.rollback()
    finish_recovery(db, recovery.id)
    db.commit()
    active = db.get(AdapterRegistry, registry.id, populate_existing=True)
    assert active.active_release_id == repaired.id and active.release_generation == 1
    assert incident.status == "failed" and incident.details["rollback_skipped"] == "recovery_generation_missing"


def test_pre_generation_candidate_cannot_invent_a_current_generation(db):
    registry = ensure_registry(db, "boss")
    db.commit()
    baseline_id = registry.active_release_id
    release = candidate(db, "legacy-candidate-without-generation")
    release.replaces_generation = None
    db.commit()
    with pytest.raises(CollectionError) as exc:
        activate_release(db, release.id)
    assert exc.value.code == "release_conflict"
    db.rollback()
    with session_factory()() as current:
        registry = current.get(AdapterRegistry, registry.id)
        assert registry.active_release_id == baseline_id and registry.release_generation == 0
