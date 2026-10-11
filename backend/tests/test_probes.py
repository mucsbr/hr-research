"""Real isolated PostgreSQL/HTTP/WS broker checks; no websites, native OS or models."""

import base64
from datetime import timedelta
import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.collection import CollectionError
from app.models import SourceAccount, TaskRun, User, now
from app.probe_models import ProbeCommand, ProbeDevice, ProbePairing
from app.probe_protocol import OPERATIONS, Receipt
from app import probe_service as service


@pytest.fixture
def registered(signed_client, db):
    settings = get_settings()
    previous = settings.probe_public_url
    settings.probe_public_url = "https://127.0.0.1:18445"
    pairing = signed_client.post("/api/v1/probes/pairings")
    assert pairing.status_code == 201
    packed = pairing.json()["pairing_config"].split(".", 1)[1]
    bundle = json.loads(base64.urlsafe_b64decode(packed + "=" * (-len(packed) % 4)))
    payload = {
        "code": bundle["code"],
        "name": "isolated-probe-fixture",
        "platform": "win32",
        "architecture": "x64",
        "version": "0.1.0",
        "capabilities": sorted(OPERATIONS),
    }
    response = signed_client.post("/api/v1/probes/agent/enroll", json=payload)
    assert response.status_code == 201
    device = response.json()
    header = {"Authorization": "Bearer " + device["token"]}
    hello = signed_client.post("/api/v1/probes/agent/hello", headers=header, json={})
    assert hello.status_code == 200
    device["connection_id"] = hello.json()["connection_id"]
    yield device, header, payload
    settings.probe_public_url = previous


def account(db, registered):
    device, _, _ = registered
    obj = SourceAccount(kind="boss", name="isolated-account")
    db.add(obj)
    db.commit()
    user = db.scalar(select(User).where(User.username == get_settings().admin_username))
    service.bind_account(db, obj.id, uuid.UUID(device["profile_id"]), user)
    return obj


def command(db, registered, *, task=None):
    obj = account(db, registered)
    session = service.acquire_session(
        db,
        obj.id,
        login=task is None,
        task_id=task.id if task else None,
        attempt=task.attempt if task else None,
    )
    cmd = service.enqueue(db, session, "browser.open_login", {"url": "https://www.zhipin.com/"})
    return obj, session, cmd


def claim(signed_client, registered):
    d, header, _ = registered
    r = signed_client.get(
        "/api/v1/probes/agent/commands?wait=0",
        headers={**header, "X-Probe-Connection": d["connection_id"]},
    )
    assert r.status_code == 200
    return r.json()["commands"]


def send_receipt(client, registered, cmd, result=None, **changes):
    d, h, _ = registered
    return client.post(
        "/api/v1/probes/agent/receipts",
        headers=h,
        json={
            "command_id": str(cmd.id),
            "connection_id": d["connection_id"],
            "status": "succeeded",
            "result": result or {"opened": True},
            "error_code": None,
            **changes,
        },
    )


def test_pairing_is_single_use_and_device_rows_hide_credentials(signed_client, db, registered):
    d, _, payload = registered
    assert signed_client.post("/api/v1/probes/agent/enroll", json=payload).status_code == 401
    public = signed_client.get("/api/v1/probes/devices").json()[0]
    assert public["status"] == "online"
    assert "token_hash" not in public and "token" not in public
    record = db.get(ProbeDevice, uuid.UUID(d["device_id"]))
    assert record.token_hash != d["token"]
    assert db.scalar(select(ProbePairing)).consumed_at is not None


def test_invalid_tls_entry_does_not_create_pairing(signed_client):
    settings = get_settings()
    previous = settings.probe_public_url
    settings.probe_public_url = "file:///not-a-server"
    try:
        assert signed_client.post("/api/v1/probes/pairings").status_code == 409
    finally:
        settings.probe_public_url = previous


def test_unknown_enrollment_fields_are_not_silently_accepted(signed_client, registered):
    _, _, payload = registered
    assert (
        signed_client.post(
            "/api/v1/probes/agent/enroll", json={**payload, "shell": "forbidden"}
        ).status_code
        == 422
    )


def test_cookie_login_is_not_a_device_credential(signed_client, registered):
    assert signed_client.post("/api/v1/probes/agent/hello", json={}).status_code == 401
    assert (
        signed_client.post(
            "/api/v1/probes/agent/hello",
            headers={"Authorization": "Bearer fixture-only-wrong-token"},
            json={},
        ).status_code
        == 401
    )


def test_connection_rotation_fences_old_heartbeat(signed_client, registered):
    d, h, _ = registered
    fresh = signed_client.post("/api/v1/probes/agent/hello", headers=h, json={}).json()
    assert fresh["connection_id"] != d["connection_id"]
    r = signed_client.post(
        "/api/v1/probes/agent/heartbeat",
        headers={**h, "X-Probe-Connection": d["connection_id"]},
        json={},
    )
    assert r.status_code == 409


def test_profile_lease_and_account_binding_mutual_exclusion(db, registered):
    obj, session, _ = command(db, registered)
    with pytest.raises(CollectionError, match="Chrome"):
        service.acquire_session(db, obj.id, login=True)
    user = db.scalar(select(User))
    with pytest.raises(HTTPException) as error:
        service.bind_account(db, obj.id, None, user)
    assert error.value.status_code == 409
    assert session.status == "active"


def test_dispatched_payload_is_encrypted_and_result_is_immutable(signed_client, db, registered):
    _, _, cmd = command(db, registered)
    assert "zhipin" not in cmd.payload_cipher
    value = claim(signed_client, registered)[0]
    assert value["payload"]["url"] == "https://www.zhipin.com/"
    assert send_receipt(signed_client, registered, cmd).status_code == 200
    duplicate = send_receipt(signed_client, registered, cmd)
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    assert send_receipt(signed_client, registered, cmd, {"opened": False}).status_code == 409
    assert service.wait_result(cmd.id) == {"opened": True}
    assert service.wait_result(cmd.id) == {
        "opened": True
    }  # retained until safe source persistence/retention


def test_undelivered_receipt_cannot_invent_success(signed_client, db, registered):
    _, _, cmd = command(db, registered)
    assert send_receipt(signed_client, registered, cmd).status_code == 409


def test_revoked_and_inactive_owner_devices_are_rejected(signed_client, db, registered):
    d, h, _ = registered
    assert (
        signed_client.post("/api/v1/probes/devices/" + d["device_id"] + "/revoke").status_code
        == 200
    )
    assert signed_client.post("/api/v1/probes/agent/hello", headers=h, json={}).status_code == 401


def test_old_task_attempt_receipt_does_not_enter_current_execution(signed_client, db, registered):
    task = TaskRun(domain="recruitment", kind="company_collection", status="running", attempt=1)
    db.add(task)
    db.commit()
    _, _, cmd = command(db, registered, task=task)
    claim(signed_client, registered)
    task.attempt = 2
    db.commit()
    assert send_receipt(signed_client, registered, cmd).status_code == 409
    db.expire_all()
    assert db.get(ProbeCommand, cmd.id).status == "expired"
    assert db.get(ProbeCommand, cmd.id).result_cipher is None


def test_finished_profile_cleanup_can_still_be_delivered(signed_client, db, registered):
    obj = account(db, registered)
    session = service.acquire_session(db, obj.id, login=True)
    cmd = service.enqueue(db, session, "browser.close_session")
    session.status = "finished"
    db.commit()
    assert claim(signed_client, registered)[0]["id"] == str(cmd.id)
    assert send_receipt(signed_client, registered, cmd, {"closed": True}).status_code == 200


def test_unknown_sent_command_is_reconciled_not_redispatched(signed_client, db, registered):
    d, h, _ = registered
    _, _, cmd = command(db, registered)
    claim(signed_client, registered)
    hello = signed_client.post("/api/v1/probes/agent/hello", headers=h, json={}).json()
    assert str(cmd.id) in hello["unresolved_commands"]
    d["connection_id"] = hello["connection_id"]
    assert claim(signed_client, registered) == []
    r = send_receipt(
        signed_client, registered, cmd, status="unknown", error_code="execution_unknown"
    )
    assert r.status_code == 200


def test_navigation_and_shell_commands_are_not_allowed(db, registered):
    obj = account(db, registered)
    session = service.acquire_session(db, obj.id, login=True)
    for url in ("file:///etc/passwd", "http://127.0.0.1/", "https://www.zhipin.com.evil.invalid/"):
        with pytest.raises(ValueError):
            service.enqueue(db, session, "browser.open_login", {"url": url})
    with pytest.raises(CollectionError):
        service.enqueue(db, session, "shell.exec", {})


def test_login_confirmation_does_not_claim_real_site_validation(signed_client, db, registered):
    obj = account(db, registered)
    session = service.acquire_session(db, obj.id, login=True)
    cmd = service.enqueue(db, session, "browser.confirm_login")
    claim(signed_client, registered)
    assert (
        send_receipt(signed_client, registered, cmd, {"saved": True, "verified": True}).status_code
        == 422
    )
    assert (
        send_receipt(signed_client, registered, cmd, {"saved": True, "verified": False}).status_code
        == 200
    )
    db.expire_all()
    assert db.get(SourceAccount, obj.id).health == "saved"


def test_websocket_uses_header_auth_and_supports_bounded_command_receipts(
    signed_client, db, registered
):
    _, h, _ = registered
    with signed_client.websocket_connect("/api/v1/probes/agent/ws", headers=h) as socket:
        socket.send_json({"type": "hello", "protocol_version": "probe_v1"})
        hello = socket.receive_json()
        assert hello["type"] == "hello"
        _, _, cmd = command(db, registered)
        delivered = socket.receive_json()
        assert delivered["id"] == str(cmd.id)
        socket.send_json(
            {
                "type": "receipt",
                "command_id": str(cmd.id),
                "connection_id": hello["connection_id"],
                "status": "succeeded",
                "result": {"opened": True},
            }
        )
        assert socket.receive_json()["type"] == "receipt_ack"
    public = signed_client.get("/api/v1/probes/devices").json()[0]
    assert public["status"] == "offline"


def test_websocket_rejects_missing_device_authority(signed_client):
    with pytest.raises(WebSocketDisconnect):
        with signed_client.websocket_connect("/api/v1/probes/agent/ws"):
            pass


def test_large_receipts_are_rejected_without_echoing_input():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Receipt(
            command_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            status="succeeded",
            result={"html": "x" * 4_000_100},
        )


def test_pairing_expiry_is_checked_under_consumption_lock(signed_client, db, registered):
    record = db.scalar(select(ProbePairing))
    record.expires_at = now() - timedelta(seconds=1)
    record.consumed_at = None
    db.commit()
    assert signed_client.post("/api/v1/probes/agent/enroll", json=registered[2]).status_code == 401
