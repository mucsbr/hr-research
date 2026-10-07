from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy import func, select

from app.db import session_factory
from app.models import RunBatch, TaskRun
from app.schemas import RunInput
from app.tasks import create_batch


def company(client, name):
    response = client.post("/api/v1/companies", json={"name": name})
    assert response.status_code == 201
    value = response.json()
    assert client.post("/api/v1/source-bindings", json={
        "company_id": value["id"], "kind": "careers", "entry_url": "https://example.com/jobs",
    }).status_code == 201
    return value


def test_main_quick_company_route_replays_header_even_after_completion(signed_client, db):
    target = company(signed_client, "幂等甲")
    path = f'/api/v1/companies/{target["id"]}/recruitment-runs'
    headers = {"Idempotency-Key": "company-click-1"}
    first = signed_client.post(path, headers=headers)
    again = signed_client.post(path, headers=headers)
    assert first.status_code == again.status_code == 202
    assert first.json()["batch_id"] == again.json()["batch_id"] and again.json()["created"] is False
    task = db.scalar(select(TaskRun))
    task.status = "succeeded"
    db.commit()
    replay = signed_client.post(path, headers=headers)
    assert replay.json()["batch_id"] == first.json()["batch_id"]
    distinct = signed_client.post(path, headers={"Idempotency-Key": "company-click-2"})
    assert distinct.json()["batch_id"] != first.json()["batch_id"]
    assert db.scalar(select(func.count()).select_from(RunBatch)) == 2


def test_same_header_conflicting_company_filters_or_name_returns_409(signed_client, db):
    first, second = company(signed_client, "幂等甲"), company(signed_client, "幂等乙")
    headers = {"Idempotency-Key": "fixed-action"}
    payload = {"company_ids": [first["id"]], "name": "一次观察", "filters": {"cities": ["上海"]}}
    assert signed_client.post("/api/v1/recruitment/runs", json=payload, headers=headers).status_code == 202
    for different in [
        {**payload, "company_ids": [second["id"]]},
        {**payload, "filters": {"cities": ["北京"]}},
        {**payload, "name": "不同请求"},
    ]:
        assert signed_client.post("/api/v1/recruitment/runs", json=different, headers=headers).status_code == 409
    assert db.scalar(select(func.count()).select_from(RunBatch)) == 1


def test_company_and_filter_order_do_not_change_set_semantics(signed_client, db):
    first, second = company(signed_client, "甲"), company(signed_client, "乙")
    headers = {"Idempotency-Key": "set-order"}
    one = signed_client.post("/api/v1/recruitment/runs", headers=headers, json={
        "company_ids": [first["id"], second["id"], first["id"]], "filters": {"cities": ["上海", "北京"]}})
    two = signed_client.post("/api/v1/recruitment/runs", headers=headers, json={
        "company_ids": [second["id"], first["id"]], "filters": {"cities": ["北京", "上海", "上海"]}})
    assert one.status_code == two.status_code == 202
    assert one.json()["batch_id"] == two.json()["batch_id"]
    assert db.scalar(select(func.count()).select_from(TaskRun)) == 2


def test_legacy_no_header_reuses_only_inflight_matching_work(signed_client, db):
    first, second = company(signed_client, "甲"), company(signed_client, "乙")
    payload = {"company_ids": [first["id"]], "filters": {"keywords": ["研发"]}}
    initial = signed_client.post("/api/v1/recruitment/runs", json=payload).json()
    repeated = signed_client.post("/api/v1/recruitment/runs", json=payload).json()
    assert initial["batch_id"] == repeated["batch_id"] and repeated["created"] is False
    for changed in [{**payload, "company_ids": [second["id"]]}, {**payload, "filters": {"keywords": ["销售"]}}]:
        assert signed_client.post("/api/v1/recruitment/runs", json=changed).json()["batch_id"] != initial["batch_id"]
    task = db.scalar(select(TaskRun).where(TaskRun.batch_id == uuid.UUID(initial["batch_id"])))
    task.status = "running"
    db.commit()
    assert signed_client.post("/api/v1/recruitment/runs", json=payload).json()["batch_id"] == initial["batch_id"]
    task.status = "succeeded"
    db.commit()
    assert signed_client.post("/api/v1/recruitment/runs", json=payload).json()["batch_id"] != initial["batch_id"]


def test_quick_plan_route_supports_key_and_detects_plan_parameter_change(signed_client, db):
    target = company(signed_client, "计划甲")
    plan_data = {"name": "重点招聘", "company_ids": [target["id"]], "filters": {"cities": ["上海"]}, "enabled": False}
    plan = signed_client.post("/api/v1/plans", json=plan_data).json()
    path = f'/api/v1/plans/{plan["id"]}/run'
    headers = {"Idempotency-Key": "plan-click-1"}
    first = signed_client.post(path, headers=headers)
    second = signed_client.post(path, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["batch_id"] == second.json()["batch_id"]
    assert signed_client.put(f'/api/v1/plans/{plan["id"]}', json={**plan_data, "filters": {"cities": ["北京"]}}).status_code == 200
    assert signed_client.post(path, headers=headers).status_code == 409


def test_legacy_company_and_plan_shortcuts_reuse_but_not_across_plan_scope(signed_client, db):
    target = company(signed_client, "公司甲")
    company_path = f'/api/v1/companies/{target["id"]}/recruitment-runs'
    first = signed_client.post(company_path).json()
    assert signed_client.post(company_path).json()["batch_id"] == first["batch_id"]
    plan = signed_client.post("/api/v1/plans", json={"name": "单独计划", "company_ids": [target["id"]]}).json()
    plan_path = f'/api/v1/plans/{plan["id"]}/run'
    planned = signed_client.post(plan_path).json()
    assert planned["batch_id"] != first["batch_id"]
    assert signed_client.post(plan_path).json()["batch_id"] == planned["batch_id"]


def test_main_api_concurrent_duplicate_requests_share_one_database_batch(signed_client, db):
    target = company(signed_client, "并发甲")
    path = f'/api/v1/companies/{target["id"]}/recruitment-runs'
    barrier = Barrier(2)
    def send(_):
        barrier.wait(timeout=3)
        return signed_client.post(path, headers={"Idempotency-Key": "concurrent-click"})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert all(response.status_code == 202 for response in results)
    assert len({response.json()["batch_id"] for response in results}) == 1
    assert sorted(response.json()["created"] for response in results) == [False, True]
    assert db.scalar(select(func.count()).select_from(RunBatch)) == 1


def test_main_api_concurrent_legacy_requests_are_atomic(signed_client, db):
    target = company(signed_client, "旧客户端并发")
    path = f'/api/v1/companies/{target["id"]}/recruitment-runs'
    barrier = Barrier(2)
    def send(_):
        barrier.wait(timeout=3)
        return signed_client.post(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert all(response.status_code == 202 for response in results)
    assert len({response.json()["batch_id"] for response in results}) == 1
    assert db.scalar(select(func.count()).select_from(TaskRun)) == 1


def test_concurrent_same_key_different_scope_creates_only_one_request(signed_client, db):
    first, second = company(signed_client, "并发甲"), company(signed_client, "并发乙")
    barrier = Barrier(2)
    def send(target):
        barrier.wait(timeout=3)
        return signed_client.post("/api/v1/recruitment/runs", headers={"Idempotency-Key": "cross-scope-race"},
                                  json={"company_ids": [target["id"]]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, [first, second]))
    assert sorted(response.status_code for response in results) == [202, 409]
    assert db.scalar(select(func.count()).select_from(RunBatch)) == 1


def test_header_body_key_conflict_is_rejected_without_creating_work(signed_client, db):
    target = company(signed_client, "甲")
    response = signed_client.post("/api/v1/recruitment/runs", headers={"Idempotency-Key": "header-key"},
                                  json={"company_ids": [target["id"]], "idempotency_key": "body-key"})
    assert response.status_code == 409
    assert db.scalar(select(func.count()).select_from(RunBatch)) == 0


def test_old_batch_without_signature_remains_replayable(signed_client, db):
    target = company(signed_client, "升级前任务")
    path = f'/api/v1/companies/{target["id"]}/recruitment-runs'
    first = signed_client.post(path, headers={"Idempotency-Key": "pre-upgrade-key"}).json()
    batch = db.get(RunBatch, uuid.UUID(first["batch_id"]))
    batch.config_snapshot = {key: value for key, value in batch.config_snapshot.items() if key != "request_parameters_v1"}
    db.commit()
    replay = signed_client.post(path, headers={"Idempotency-Key": "pre-upgrade-key"})
    assert replay.status_code == 202 and replay.json()["batch_id"] == first["batch_id"]


def test_resume_snapshot_is_atomic_and_predecessor_is_part_of_key_identity(signed_client, db):
    target = company(signed_client, "恢复甲")
    company_id = uuid.UUID(target["id"])
    previous = TaskRun(company_id=company_id, status="failed")
    other = TaskRun(company_id=company_id, status="failed")
    db.add_all([previous, other])
    db.commit()
    request = RunInput(company_ids=[company_id], idempotency_key="resume-one")
    batch, created = create_batch(db, request, resume_from_task_id=previous.id)
    assert created
    # A different worker/session sees the predecessor in the same committed task creation.
    with session_factory()() as reader:
        task = reader.scalar(select(TaskRun).where(TaskRun.batch_id == batch.id))
        assert task.config_snapshot["resume_from_task_id"] == str(previous.id)
    assert create_batch(db, request, resume_from_task_id=previous.id)[0].id == batch.id
    with pytest.raises(HTTPException) as exc:
        create_batch(db, request, resume_from_task_id=other.id)
    assert exc.value.status_code == 409
    db.rollback()


def test_legacy_active_reuse_does_not_rewrite_existing_resume_snapshot(signed_client, db):
    target = company(signed_client, "恢复甲")
    company_id = uuid.UUID(target["id"])
    previous = TaskRun(company_id=company_id, status="failed")
    other = TaskRun(company_id=company_id, status="failed")
    db.add_all([previous, other])
    db.commit()
    request = RunInput(company_ids=[company_id])
    batch, _ = create_batch(db, request, resume_from_task_id=previous.id)
    replay, created = create_batch(db, request, resume_from_task_id=other.id)
    assert not created and replay.id == batch.id
    task = db.scalar(select(TaskRun).where(TaskRun.batch_id == batch.id))
    assert task.config_snapshot["resume_from_task_id"] == str(previous.id)
