"""User device management and terminal-initiated authenticated transports."""

import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import Field, ValidationError
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.collection import CollectionError
from app.config import get_settings
from app.db import session_factory
from app.models import AuditEvent, SourceAccount
from app.probe_models import (
    ProbeAccountBinding,
    ProbeBrowserSession,
    ProbeCommand,
    ProbeDevice,
    ProbeProfile,
)
from app.probe_protocol import Enrollment, MAX_MESSAGE_BYTES, ProbeHello, ProbeInput, Receipt
from app import probe_service as service
from app.security import AdminUser, CurrentUser, Db

router = APIRouter(prefix="/api/v1/probes")


def bearer(value):
    if not value or not value.startswith("Bearer "):
        raise HTTPException(401, "需要探针身份")
    return value[7:]


class ProfileInput(ProbeInput):
    device_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)


class BindingInput(ProbeInput):
    profile_id: uuid.UUID | None


class NativeLoginInput(ProbeInput):
    profile_id: uuid.UUID
    kind: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=120)


def collection_error(error):
    return HTTPException(409, {"code": error.code, "message": str(error)})


@router.get("/devices")
def devices(db: Db, user: CurrentUser):
    query = select(ProbeDevice).order_by(ProbeDevice.created_at.desc())
    if user.role != "admin":
        query = query.where(ProbeDevice.owner_user_id == user.id)
    return [service.public_device(d) for d in db.scalars(query)]


@router.post("/pairings", status_code=201)
def create_pairing(db: Db, user: CurrentUser):
    return service.pairing(db, user)


@router.get("/profiles")
def profiles(db: Db, user: CurrentUser):
    query = select(ProbeProfile, ProbeDevice).join(ProbeDevice).order_by(ProbeProfile.created_at)
    if user.role != "admin":
        query = query.where(ProbeDevice.owner_user_id == user.id)
    return [
        {
            "id": str(p.id),
            "device_id": str(d.id),
            "device_name": d.name,
            "name": p.name,
            "enabled": p.enabled and d.enabled,
            "device_status": service.public_device(d)["status"],
        }
        for p, d in db.execute(query)
    ]


@router.post("/profiles", status_code=201)
def add_profile(data: ProfileInput, db: Db, user: CurrentUser):
    device = service.require_owner(db, data.device_id, user)
    if not device.enabled:
        raise HTTPException(409, "设备已撤销")
    profile = ProbeProfile(device_id=device.id, name=data.name.strip())
    db.add(profile)
    db.commit()
    return {"id": str(profile.id), "device_id": str(device.id), "name": profile.name}


@router.put("/accounts/{account_id}")
def bind_account(account_id: uuid.UUID, data: BindingInput, db: Db, user: AdminUser):
    return {"probe": service.bind_account(db, account_id, data.profile_id, user)}


@router.post("/devices/{device_id}/revoke")
def revoke(device_id: uuid.UUID, db: Db, user: CurrentUser):
    device = service.require_owner(db, device_id, user)
    device.enabled, device.connection_id = False, None
    profile_ids = select(ProbeProfile.id).where(ProbeProfile.device_id == device.id)
    for session in db.scalars(
        select(ProbeBrowserSession).where(
            ProbeBrowserSession.profile_id.in_(profile_ids), ProbeBrowserSession.status == "active"
        )
    ):
        session.status = "revoked"
    db.add(AuditEvent(user_id=user.id, action="probe.revoked", target=str(device.id)))
    db.commit()
    return {"revoked": True}


def login_command(db, account_id):
    from app.browser_sessions import PLATFORMS

    session = service.acquire_session(db, account_id, login=True)
    command = service.enqueue(
        db, session, "browser.open_login", {"url": PLATFORMS[session.kind][0]}
    )
    return {
        "session_id": str(session.id),
        "command_id": str(command.id),
        "mode": "probe",
        "expires_at": session.expires_at,
    }


@router.post("/accounts/{account_id}/login", status_code=202)
def open_login(account_id: uuid.UUID, db: Db, user: AdminUser):
    descriptor = service.snapshot_for_account(db, account_id)
    if not descriptor:
        raise HTTPException(409, "该账号尚未绑定探针")
    service.require_owner(db, uuid.UUID(descriptor["device_id"]), user)
    try:
        return login_command(db, account_id)
    except CollectionError as error:
        raise collection_error(error) from None


@router.post("/sessions/{session_id}/confirm-login", status_code=202)
def confirm_login(session_id: uuid.UUID, db: Db, user: AdminUser):
    session = db.get(ProbeBrowserSession, session_id)
    if not session or session.task_id:
        raise HTTPException(404, "人工登录会话不存在")
    profile = db.get(ProbeProfile, session.profile_id)
    service.require_owner(db, profile.device_id, user)
    try:
        command = service.enqueue(db, session, "browser.confirm_login")
        return {"command_id": str(command.id)}
    except CollectionError as error:
        raise collection_error(error) from None


@router.get("/commands/{command_id}")
def command_status(command_id: uuid.UUID, db: Db, user: CurrentUser):
    command = db.get(ProbeCommand, command_id)
    if not command:
        raise HTTPException(404, "命令不存在")
    service.require_owner(db, command.device_id, user)
    return {
        "id": str(command.id),
        "operation": command.operation,
        "status": command.status,
        "error_code": command.error_code,
        "expires_at": command.expires_at,
        "finished_at": command.finished_at,
    }


@router.post("/sessions/{session_id}/close")
def close_login(session_id: uuid.UUID, db: Db, user: AdminUser):
    session = db.get(ProbeBrowserSession, session_id)
    if not session or session.task_id:
        raise HTTPException(404, "人工登录会话不存在")
    profile = db.get(ProbeProfile, session.profile_id)
    service.require_owner(db, profile.device_id, user)
    if session.status == "active":
        try:
            service.enqueue(db, session, "browser.close_session")
        except CollectionError:
            pass
        session.status = "finished"
        db.commit()
    return {"closed": True, "chrome_preserved": True}


@router.post("/agent/enroll", status_code=201)
def enroll(data: Enrollment, db: Db):
    return service.enroll(db, data)


@router.post("/agent/hello")
def agent_hello(data: ProbeHello, db: Db, authorization: str | None = Header(default=None)):
    return service.hello(db, service.require_device(db, bearer(authorization)), data)


@router.post("/agent/unpair")
def native_unpair(db: Db, authorization: str | None = Header(default=None)):
    device = service.require_device(db, bearer(authorization))
    device.enabled, device.connection_id = False, None
    ids = select(ProbeProfile.id).where(ProbeProfile.device_id == device.id)
    for session in db.scalars(
        select(ProbeBrowserSession).where(
            ProbeBrowserSession.profile_id.in_(ids), ProbeBrowserSession.status == "active"
        )
    ):
        session.status = "revoked"
    db.add(
        AuditEvent(
            user_id=device.owner_user_id, action="probe.unpaired_by_device", target=str(device.id)
        )
    )
    db.commit()
    return {"revoked": True}


def claim(token, connection_id, touch=False):
    with session_factory()() as db:
        device = service.require_device(db, token)
        return service.claim_commands(db, device, connection_id, touch=touch)


@router.get("/agent/commands")
async def agent_commands(
    authorization: str | None = Header(default=None),
    x_probe_connection: uuid.UUID = Header(),
    wait: int = Query(20, ge=0, le=25),
):
    token = bearer(authorization)
    deadline = time.monotonic() + wait
    touch = True
    while True:
        commands = await run_in_threadpool(claim, token, x_probe_connection, touch)
        touch = False
        if commands or time.monotonic() >= deadline:
            return {"commands": commands}
        await asyncio.sleep(0.5)


@router.post("/agent/heartbeat")
def agent_heartbeat(
    data: ProbeHello,
    db: Db,
    x_probe_connection: uuid.UUID = Header(),
    authorization: str | None = Header(default=None),
):
    service.heartbeat(
        db, service.require_device(db, bearer(authorization)), x_probe_connection, data
    )
    return {"ok": True}


@router.post("/agent/receipts")
def agent_receipt(data: Receipt, db: Db, authorization: str | None = Header(default=None)):
    return service.receipt(db, service.require_device(db, bearer(authorization)), data)


@router.post("/agent/open-login", status_code=202)
def native_open_login(
    data: NativeLoginInput, db: Db, authorization: str | None = Header(default=None)
):
    from app.browser_sessions import PLATFORMS

    device = service.require_device(db, bearer(authorization))
    profile = db.scalar(
        select(ProbeProfile)
        .where(ProbeProfile.id == data.profile_id, ProbeProfile.device_id == device.id)
        .with_for_update()
    )
    if not profile or not profile.enabled or data.kind not in PLATFORMS:
        raise HTTPException(404, "浏览器或平台不可用")
    account = db.scalar(
        select(SourceAccount)
        .join(ProbeAccountBinding, ProbeAccountBinding.account_id == SourceAccount.id)
        .where(ProbeAccountBinding.profile_id == profile.id, SourceAccount.kind == data.kind)
    )
    if account is None:
        account = SourceAccount(kind=data.kind, name=data.name.strip())
        db.add(account)
        db.flush()
        db.add(ProbeAccountBinding(account_id=account.id, profile_id=profile.id))
        db.flush()
        db.add(
            AuditEvent(
                user_id=device.owner_user_id,
                action="probe.account_created",
                target=str(account.id),
                detail={"device_id": str(device.id), "kind": data.kind},
            )
        )
    try:
        return {**login_command(db, account.id), "account_id": str(account.id)}
    except CollectionError as error:
        raise collection_error(error) from None


@router.post("/agent/sessions/{session_id}/confirm-login", status_code=202)
def native_confirm_login(
    session_id: uuid.UUID, db: Db, authorization: str | None = Header(default=None)
):
    device = service.require_device(db, bearer(authorization))
    session = db.get(ProbeBrowserSession, session_id)
    profile = db.get(ProbeProfile, session.profile_id) if session else None
    if not profile or profile.device_id != device.id or session.task_id:
        raise HTTPException(404, "人工登录会话不存在")
    try:
        return {"command_id": str(service.enqueue(db, session, "browser.confirm_login").id)}
    except CollectionError as error:
        raise collection_error(error) from None


@router.websocket("/agent/ws")
async def agent_socket(socket: WebSocket):
    # Device tokens are headers, never URL query strings or browser cookies.
    try:
        token = bearer(socket.headers.get("authorization"))
        with session_factory()() as db:
            device = service.require_device(db, token)
            device_id = device.id
    except HTTPException:
        await socket.close(code=1008)
        return
    origin = socket.headers.get("origin")
    allowed = {get_settings().app_url.rstrip("/"), get_settings().probe_public_url.rstrip("/")}
    if origin and origin.rstrip("/") not in allowed:
        await socket.close(code=1008)
        return
    await socket.accept()
    sender = None
    connection_id = None
    lock = asyncio.Lock()

    async def send(message):
        async with lock:
            await socket.send_json(message)

    async def receive():
        value = await socket.receive_text()
        if len(value.encode()) > MAX_MESSAGE_BYTES:
            raise ValueError("message_too_large")
        obj = json.loads(value)
        if not isinstance(obj, dict):
            raise ValueError("message_invalid")
        return obj

    async def send_commands():
        while True:
            commands = await run_in_threadpool(claim, token, connection_id)
            for command in commands:
                await send(command)
            await asyncio.sleep(0.5)

    try:
        first = await asyncio.wait_for(receive(), timeout=10)
        if first.pop("type", None) != "hello":
            raise ValueError("hello_required")
        data = ProbeHello.model_validate(first)
        with session_factory()() as db:
            hello = service.hello(db, service.require_device(db, token), data)
        connection_id = uuid.UUID(hello["connection_id"])
        await send(hello)
        sender = asyncio.create_task(send_commands())
        while True:
            # A terminal that stops heartbeats cannot keep an authenticated command lease forever.
            message = await asyncio.wait_for(receive(), timeout=50)
            kind = message.pop("type", None)
            with session_factory()() as db:
                current = service.require_device(db, token)
                if kind == "heartbeat":
                    epoch = uuid.UUID(message.pop("connection_id"))
                    service.heartbeat(db, current, epoch, ProbeHello.model_validate(message))
                    response = {"type": "heartbeat", "ok": True}
                elif kind == "receipt":
                    response = {
                        "type": "receipt_ack",
                        "command_id": message.get("command_id"),
                        **service.receipt(db, current, Receipt.model_validate(message)),
                    }
                else:
                    raise ValueError("message_type_invalid")
            await send(response)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except (ValueError, ValidationError, HTTPException):
        try:
            await socket.close(code=1008)
        except RuntimeError:
            pass
    finally:
        if sender:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        with session_factory()() as db:
            device = db.get(ProbeDevice, device_id)
            if device and device.connection_id == connection_id:
                device.last_seen_at = None
                db.commit()


def release_manifest():
    root = get_settings().probe_downloads_dir
    manifest = root / "manifest.json"
    if not manifest.is_file() or manifest.is_symlink():
        return []
    rows = json.loads(manifest.read_text())
    if not isinstance(rows, list) or len(rows) > 8:
        raise HTTPException(503, "探针发布清单无效")
    result = []
    for row in rows:
        filename = row.get("filename", "")
        if Path(filename).name != filename or not filename.endswith((".exe", ".zip", ".dmg")):
            raise HTTPException(503, "探针安装包路径无效")
        path = root / filename
        if not path.is_file() or path.is_symlink() or path.resolve().parent != root.resolve():
            continue
        if row.get("platform") not in {"win32", "darwin"} or row.get("architecture") not in {
            "x64",
            "arm64",
        }:
            raise HTTPException(503, "探针目标系统无效")
        result.append(
            {
                key: row[key]
                for key in (
                    "id",
                    "filename",
                    "platform",
                    "architecture",
                    "version",
                    "sha256",
                    "signature_status",
                    "native_validation",
                )
            }
        )
    return result


@router.get("/downloads")
def downloads(user: CurrentUser):
    return {
        "releases": release_manifest(),
        "protocol_version": "probe_v1",
        "server": get_settings().probe_public_url or None,
    }


@router.get("/downloads/{release_id}")
def download(release_id: str, user: CurrentUser):
    release = next((row for row in release_manifest() if row["id"] == release_id), None)
    if not release:
        raise HTTPException(404, "这个系统的探针安装包尚未发布")
    path = get_settings().probe_downloads_dir / release["filename"]
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != release["sha256"]:
        raise HTTPException(503, "探针安装包校验不一致，已拒绝下载")
    return FileResponse(path, filename=release["filename"], media_type="application/octet-stream")
