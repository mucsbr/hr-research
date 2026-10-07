"""Exercise the actual dispatcher against PostgreSQL without broker/model/network calls."""
import uuid

import pytest
from sqlalchemy import select

from app.adapter_code_models import AdapterCodeProposal
from app.adapter_maintenance import ensure_registry
from app.maintenance_models import AdapterMaintenanceRun
from app.models import TaskRun
from app import workers
from app.collection import CollectionError


def proposal(db, registry, status, *, incident=False, automatic=True, origin="failure_evidence"):
    task = TaskRun(domain="maintenance", kind="adapter_code_upgrade")
    db.add(task)
    db.flush()
    row = AdapterCodeProposal(task_id=task.id, registry_id=registry.id, base_release_id=registry.active_release_id,
        base_generation=registry.release_generation, dedupe_key=uuid.uuid4().hex, status=status, origin=origin,
        source_snapshot={"automatic": automatic, **({"incident_id": str(uuid.uuid4())} if incident else {})})
    db.add(row)
    db.flush()
    return row


def recorder(monkeypatch):
    calls = {}
    for name in ("check_adapter", "repair_adapter", "queue_adapter_code_upstream", "maintain_adapter_code"):
        calls[name] = []
        def record(*, args, queue, name=name):
            calls[name].append((args[0], queue))
        monkeypatch.setattr(getattr(workers, name), "apply_async", record)
    return calls


def test_dispatch_skips_settled_history_and_respects_independent_code_controls(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.code_agent_enabled = True
    # Older settled rows must not starve the pending candidates behind a query limit.
    for _ in range(15):
        proposal(db, registry, "published")
    ready = proposal(db, registry, "queued", incident=True)
    waiting = proposal(db, registry, "awaiting_publish", incident=True)
    proposal(db, registry, "recovery_queued", incident=True)
    proposal(db, registry, "queued", automatic=False)
    upstream = proposal(db, registry, "queued", origin="fixed_upstream")
    registry.code_auto_publish_enabled = False
    registry.code_upstream_enabled = False
    db.commit()
    calls = recorder(monkeypatch)
    workers.dispatch_maintenance()
    assert calls["maintain_adapter_code"] == [(str(ready.id), "maintenance")]
    registry.code_auto_publish_enabled = True
    registry.code_upstream_enabled = True
    db.commit()
    calls["maintain_adapter_code"].clear()
    workers.dispatch_maintenance()
    assert {identifier for identifier, _ in calls["maintain_adapter_code"]} == {str(ready.id), str(waiting.id), str(upstream.id)}


def test_dispatch_recovers_upstream_commit_to_queue_gap_without_fetching_in_dispatch(db, monkeypatch):
    registry = ensure_registry(db, "boss")
    registry.code_agent_enabled = registry.code_upstream_enabled = True
    todo = AdapterMaintenanceRun(registry_id=registry.id, kind="upstream_check", status="needs_engineering")
    done = AdapterMaintenanceRun(registry_id=registry.id, kind="upstream_check", status="needs_engineering", details={"code_handled": True})
    db.add_all([todo, done])
    db.commit()
    calls = recorder(monkeypatch)
    workers.dispatch_maintenance()
    assert calls["queue_adapter_code_upstream"] == [(str(todo.id), "maintenance")]


def test_check_worker_dispatches_code_sampling_only_after_check_returns(monkeypatch):
    calls = recorder(monkeypatch)
    order = []
    monkeypatch.setattr("app.adapter_maintenance.execute_check", lambda identifier: order.append(identifier))
    workers.check_adapter("fixed-check-id")
    assert order == ["fixed-check-id"]
    assert calls["queue_adapter_code_upstream"] == [("fixed-check-id", "maintenance")]


@pytest.mark.parametrize("status", ["recovery_queued", "recovering"])
def test_published_recovery_does_not_offer_false_proposal_cancellation(db, status):
    from app.adapter_code_service import cancel_code_proposal
    import test_adapter_code_runtime as runtime_support
    state = runtime_support.seed_runtime(db)
    item = db.scalar(select(AdapterCodeProposal).where(AdapterCodeProposal.release_id == state.release.id))
    item.status = status
    db.commit()
    with pytest.raises(CollectionError) as error:
        cancel_code_proposal(item.id, client=state.runner)
    db.refresh(item)
    db.refresh(state.registry)
    assert error.value.code == "proposal_published"
    assert not item.cancel_requested and item.status == status
    assert state.registry.active_release_id == state.release.id
