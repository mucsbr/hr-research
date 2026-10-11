"""Durable, authenticated outbound command broker shared by HTTP and WebSocket."""

import base64
from datetime import timedelta
import hashlib
import hmac
import json
import secrets
import time
import uuid
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.browser_sessions import PLATFORMS, cipher
from app.collection import CollectionError
from app.config import get_settings
from app.db import session_factory
from app.models import AuditEvent, ModelCall, SourceAccount, TaskEvent, TaskRun, User, now
from app.probe_models import (
    ProbeAccountBinding,
    ProbeBrowserSession,
    ProbeCommand,
    ProbeDevice,
    ProbePairing,
    ProbeProfile,
)
from app.probe_protocol import PROTOCOL, digest, json_bytes, validate_operation, validate_url

TERMINAL = {"succeeded", "failed", "unknown", "expired", "cancelled"}


def seal(value):
    return cipher().encrypt(json_bytes(value)).decode()


def unseal(value):
    return json.loads(cipher().decrypt(value.encode()))


def secret_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def public_device(device):
    online = bool(
        device.last_seen_at
        and device.last_seen_at > now() - timedelta(seconds=get_settings().probe_online_seconds)
    )
    return {
        "id": str(device.id),
        "name": device.name,
        "platform": device.platform,
        "architecture": device.architecture,
        "version": device.version,
        "protocol_version": device.protocol_version,
        "owner_user_id": str(device.owner_user_id),
        "enabled": device.enabled,
        "paused": device.paused,
        "last_seen_at": device.last_seen_at,
        "chrome_version": device.chrome_version,
        "capabilities": device.capabilities,
        "status": "revoked"
        if not device.enabled
        else "paused"
        if device.paused
        else "online"
        if online
        else "offline",
    }


def require_device(db, token):
    if not token or not 20 <= len(token) <= 200:
        raise HTTPException(401, "探针身份无效")
    device = db.scalar(select(ProbeDevice).where(ProbeDevice.token_hash == secret_hash(token)))
    if (
        not device
        or not device.enabled
        or not hmac.compare_digest(device.token_hash, secret_hash(token))
    ):
        raise HTTPException(401, "探针身份已失效或已撤销")
    owner = db.get(User, device.owner_user_id)
    if not owner or not owner.active:
        raise HTTPException(401, "探针所属用户已停用")
    return device


def require_owner(db, device_id, user):
    device = db.get(ProbeDevice, device_id)
    if not device or (user.role != "admin" and device.owner_user_id != user.id):
        raise HTTPException(404, "采集设备不存在")
    return device


def pairing(db, user):
    settings = get_settings()
    endpoint = settings.probe_public_url.rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != "https" and settings.app_env != "test")
    ):
        raise HTTPException(409, "请先配置探针使用的可信HTTPS入口")
    code = secrets.token_urlsafe(32)
    expires = now() + timedelta(seconds=settings.probe_pairing_seconds)
    db.add(ProbePairing(owner_user_id=user.id, code_hash=secret_hash(code), expires_at=expires))
    db.add(AuditEvent(user_id=user.id, action="probe.pairing_created", target="probe_v1"))
    db.commit()
    ca = settings.probe_ca_file.read_text() if settings.probe_ca_file else None
    bundle = {
        "protocol_version": PROTOCOL,
        "server": endpoint,
        "code": code,
        "ca_pem": ca,
        "expires_at": expires.isoformat(),
    }
    packed = "hrprobe1." + base64.urlsafe_b64encode(json_bytes(bundle)).decode().rstrip("=")
    return {"pairing_config": packed, "server": endpoint, "expires_at": expires}


def enroll(db, data):
    record = db.scalar(
        select(ProbePairing)
        .where(ProbePairing.code_hash == secret_hash(data.code))
        .with_for_update()
    )
    if not record or record.consumed_at or record.expires_at <= now():
        raise HTTPException(401, "配对码无效、已使用或已过期")
    owner = db.get(User, record.owner_user_id)
    if not owner or not owner.active:
        raise HTTPException(401, "配对用户已停用")
    token = secrets.token_urlsafe(48)
    device = ProbeDevice(
        owner_user_id=record.owner_user_id,
        name=data.name.strip(),
        platform=data.platform,
        architecture=data.architecture,
        version=data.version,
        token_hash=secret_hash(token),
        capabilities=list(data.capabilities),
    )
    db.add(device)
    db.flush()
    profile = ProbeProfile(device_id=device.id, name="默认采集浏览器")
    db.add(profile)
    record.consumed_at = now()
    db.add(
        AuditEvent(
            user_id=owner.id,
            action="probe.enrolled",
            target=str(device.id),
            detail={"platform": data.platform, "version": data.version},
        )
    )
    db.commit()
    return {
        "device_id": str(device.id),
        "token": token,
        "profile_id": str(profile.id),
        "protocol_version": PROTOCOL,
    }


def hello(db, device, data):
    device = db.scalar(select(ProbeDevice).where(ProbeDevice.id == device.id).with_for_update())
    device.connection_id = uuid.uuid4()
    device.last_seen_at, device.paused = now(), data.paused
    device.chrome_version = data.chrome_version
    db.flush()
    if not data.paused:
        resume_never_started(db, device.id)
    unresolved = db.scalars(
        select(ProbeCommand)
        .where(ProbeCommand.device_id == device.id, ProbeCommand.status == "sent")
        .limit(50)
    ).all()
    profiles = db.scalars(
        select(ProbeProfile).where(ProbeProfile.device_id == device.id, ProbeProfile.enabled)
    ).all()
    db.commit()
    return {
        "type": "hello",
        "connection_id": str(device.connection_id),
        "protocol_version": PROTOCOL,
        "server_time": now().isoformat(),
        "heartbeat_seconds": 20,
        "profiles": [{"id": str(p.id), "name": p.name} for p in profiles],
        # These IDs are reconciled against the local encrypted journal, never blindly replayed.
        "unresolved_commands": [str(c.id) for c in unresolved],
    }


def resume_never_started(db, device_id):
    """Only queue the same never-started collection; unknown effects/model work are not replayed."""
    tasks = db.scalars(
        select(TaskRun)
        .where(
            TaskRun.status == "waiting_device",
            TaskRun.cancel_requested.is_(False),
            TaskRun.kind.in_(["company_collection", "sentiment_collection"]),
        )
        .with_for_update(skip_locked=True)
        .limit(50)
    ).all()
    for task in tasks:
        sources = task.config_snapshot.get(
            "sources_snapshot", task.config_snapshot.get("sources", [])
        )
        descriptors = [s.get("browser_session") for s in sources if s.get("browser_session")]
        if not any(d.get("device_id") == str(device_id) for d in descriptors):
            continue
        if db.scalar(select(ModelCall.id).where(ModelCall.task_id == task.id).limit(1)):
            continue
        session_ids = select(ProbeBrowserSession.id).where(ProbeBrowserSession.task_id == task.id)
        if db.scalar(
            select(ProbeCommand.id)
            .where(ProbeCommand.session_id.in_(session_ids), ProbeCommand.delivered_at.is_not(None))
            .limit(1)
        ):
            continue
        ready = True
        for source in sources:
            frozen = source.get("browser_session")
            if not frozen:
                continue
            current = snapshot_for_account(db, uuid.UUID(source["account_id"]))
            device = db.get(ProbeDevice, uuid.UUID(frozen["device_id"]))
            if frozen != current or not device or public_device(device)["status"] != "online":
                ready = False
                break
        if ready:
            task.status, task.finished_at, task.error_code, task.error_message = (
                "queued",
                None,
                None,
                None,
            )
            db.add(
                TaskEvent(
                    task_id=task.id,
                    level="info",
                    message="采集设备已恢复在线，尚未开始的任务继续排队；未重放未知操作",
                )
            )


def check_connection(device, connection_id):
    if device.connection_id != connection_id:
        raise HTTPException(409, "探针连接已被新连接替换")


def heartbeat(db, device, connection_id, data):
    check_connection(device, connection_id)
    device.last_seen_at, device.paused = now(), data.paused
    device.chrome_version = data.chrome_version
    db.commit()


def claim_commands(db, device, connection_id, *, touch=False):
    check_connection(device, connection_id)
    if touch:
        device.last_seen_at = now()
    if device.paused:
        db.commit()
        return []
    commands = db.scalars(
        select(ProbeCommand)
        .where(
            ProbeCommand.device_id == device.id,
            ProbeCommand.status == "pending",
        )
        .order_by(ProbeCommand.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).all()
    result = []
    for command in commands:
        if command.expires_at <= now():
            command.status, command.error_code = "expired", "command_expired"
            command.finished_at = now()
            continue
        session = db.get(ProbeBrowserSession, command.session_id) if command.session_id else None
        if (
            session
            and command.operation != "browser.close_session"
            and not session_valid(db, session)
        ):
            command.status, command.error_code = "cancelled", "execution_superseded"
            command.finished_at = now()
            continue
        command.status, command.connection_id, command.delivered_at = "sent", connection_id, now()
        result.append(
            {
                "type": "command",
                "id": str(command.id),
                "connection_id": str(connection_id),
                "operation": command.operation,
                "expires_at": command.expires_at.isoformat(),
                "payload": unseal(command.payload_cipher),
            }
        )
    db.commit()
    return result


def session_valid(db, session):
    if session.status != "active" or session.expires_at <= now():
        return False
    binding = db.scalar(
        select(ProbeAccountBinding).where(ProbeAccountBinding.account_id == session.account_id)
    )
    if (
        not binding
        or binding.profile_id != session.profile_id
        or binding.revision != session.binding_revision
    ):
        return False
    if session.task_id:
        from app.execution import active_execution

        try:
            task = active_execution(db, session.task_id, session.attempt)
        except CollectionError:
            return False
        if not task or task.status != "running" or task.cancel_requested:
            return False
    return True


def receipt(db, device, data):
    check_connection(device, data.connection_id)
    command = db.scalar(
        select(ProbeCommand)
        .where(ProbeCommand.id == data.command_id, ProbeCommand.device_id == device.id)
        .with_for_update()
    )
    if not command:
        raise HTTPException(404, "探针命令不存在")
    result_digest = digest(
        {"status": data.status, "result": data.result, "error_code": data.error_code}
    )
    if command.status in TERMINAL:
        if command.result_digest == result_digest:
            return {"accepted": True, "duplicate": True}
        raise HTTPException(409, "命令结果已固定，不能覆盖")
    if command.status != "sent":
        raise HTTPException(409, "命令尚未交付，不能提交回执")
    session = db.get(ProbeBrowserSession, command.session_id) if command.session_id else None
    if command.expires_at <= now() or (
        session and command.operation != "browser.close_session" and not session_valid(db, session)
    ):
        command.status, command.error_code, command.finished_at = (
            "expired",
            "execution_superseded",
            now(),
        )
        db.commit()
        raise HTTPException(409, "命令或任务执行已过期，未接纳晚到资料")
    command.status, command.error_code = data.status, data.error_code
    command.result_cipher, command.result_digest = seal(data.result), result_digest
    command.finished_at = now()
    device.last_seen_at = now()
    if session and command.operation == "browser.confirm_login" and data.status == "succeeded":
        if data.result.get("saved") is not True or data.result.get("verified") is not False:
            raise HTTPException(422, "登录回执不能把人工确认冒充已验证采集")
        account = db.get(SourceAccount, session.account_id)
        account.health = "saved"
        session.status = "finished"
    if session and command.operation == "browser.open_login" and data.status != "succeeded":
        session.status = "failed"
    db.commit()
    return {"accepted": True, "duplicate": False}


def snapshot_for_account(db, account_id):
    if not account_id:
        return None
    binding = db.scalar(
        select(ProbeAccountBinding).where(ProbeAccountBinding.account_id == account_id)
    )
    if not binding:
        return None
    profile = db.get(ProbeProfile, binding.profile_id)
    return {
        "mode": "probe",
        "device_id": str(profile.device_id),
        "profile_id": str(profile.id),
        "revision": str(binding.revision),
        "protocol_version": PROTOCOL,
    }


def bind_account(db, account_id, profile_id, user):
    account = db.get(SourceAccount, account_id)
    if not account or account.kind not in PLATFORMS:
        raise HTTPException(404, "来源账号不存在或平台不支持")
    if account.archived:
        raise HTTPException(409, "账号已归档")
    binding = db.scalar(
        select(ProbeAccountBinding)
        .where(ProbeAccountBinding.account_id == account_id)
        .with_for_update()
    )
    active = db.scalar(
        select(ProbeBrowserSession.id).where(
            ProbeBrowserSession.account_id == account_id,
            ProbeBrowserSession.status == "active",
            ProbeBrowserSession.expires_at > now(),
        )
    )
    if active:
        raise HTTPException(409, "该账号正用于登录或采集，结束后再更换设备")
    if profile_id is None:
        if binding:
            db.delete(binding)
        account.health = "logged_out"
    else:
        profile = db.get(ProbeProfile, profile_id)
        if not profile or not profile.enabled:
            raise HTTPException(404, "浏览器资料目录不存在")
        device = require_owner(db, profile.device_id, user)
        if not device.enabled:
            raise HTTPException(409, "设备已撤销")
        if binding:
            binding.profile_id, binding.revision = profile.id, uuid.uuid4()
        else:
            db.add(ProbeAccountBinding(account_id=account_id, profile_id=profile.id))
        account.health = "logged_out"
    db.add(
        AuditEvent(
            user_id=user.id,
            action="probe.account_bound",
            target=str(account_id),
            detail={"profile_id": str(profile_id) if profile_id else None},
        )
    )
    db.commit()
    return snapshot_for_account(db, account_id)


def acquire_session(db, account_id, *, frozen=None, task_id=None, attempt=None, login=False):
    descriptor = snapshot_for_account(db, account_id)
    if not descriptor:
        raise CollectionError("blocked_configuration", "账号未绑定采集设备")
    if frozen is not None and frozen != descriptor:
        raise CollectionError("blocked_configuration", "任务冻结后浏览器绑定已改变，请核对配置")
    profile = db.scalar(
        select(ProbeProfile)
        .where(ProbeProfile.id == uuid.UUID(descriptor["profile_id"]))
        .with_for_update()
    )
    device = db.scalar(
        select(ProbeDevice).where(ProbeDevice.id == profile.device_id).with_for_update()
    )
    if not profile.enabled or public_device(device)["status"] != "online":
        raise CollectionError("waiting_device", "采集设备离线、暂停或已撤销")
    account = db.get(SourceAccount, account_id)
    if not account or account.kind not in PLATFORMS:
        raise CollectionError("account_platform_mismatch", "探针账号平台不匹配")
    active = db.scalars(
        select(ProbeBrowserSession).where(
            ProbeBrowserSession.profile_id.in_(
                select(ProbeProfile.id).where(ProbeProfile.device_id == device.id)
            ),
            ProbeBrowserSession.status == "active",
        )
    ).all()
    for old in active:
        if old.expires_at <= now() or not session_valid(db, old):
            old.status = "expired"
        else:
            raise CollectionError("waiting_account", "该Chrome正用于另一任务或人工登录")
    db.flush()
    seconds = (
        get_settings().browser_login_seconds if login else get_settings().task_timeout_seconds + 60
    )
    session = ProbeBrowserSession(
        profile_id=profile.id,
        account_id=account_id,
        task_id=task_id,
        attempt=attempt,
        kind=account.kind,
        binding_revision=uuid.UUID(descriptor["revision"]),
        expires_at=now() + timedelta(seconds=seconds),
    )
    db.add(session)
    db.commit()
    return session


def enqueue(db, session, operation, payload=None):
    profile = db.get(ProbeProfile, session.profile_id)
    device = db.get(ProbeDevice, profile.device_id)
    if public_device(device)["status"] != "online":
        raise CollectionError("waiting_device", "采集设备未在线")
    if operation not in device.capabilities:
        raise CollectionError("blocked_configuration", "探针版本不支持该浏览器操作")
    if not session_valid(db, session):
        raise CollectionError("execution_superseded", "浏览器操作的任务或绑定已失效")
    roots = list(PLATFORMS[session.kind][1])
    value = {
        **(payload or {}),
        "session_id": str(session.id),
        "profile_id": str(profile.id),
        "kind": session.kind,
        "allowed_roots": roots,
        "task_id": str(session.task_id) if session.task_id else None,
        "attempt": session.attempt,
        "session_expires_at": session.expires_at.isoformat(),
    }
    if value.get("url"):
        validate_url(value["url"], roots)
    validate_operation(operation, value)
    command = ProbeCommand(
        device_id=device.id,
        session_id=session.id,
        operation=operation,
        payload_cipher=seal(value),
        expires_at=now() + timedelta(seconds=get_settings().probe_command_timeout_seconds),
    )
    db.add(command)
    db.commit()
    return command


def wait_result(command_id):
    deadline = time.monotonic() + get_settings().probe_command_timeout_seconds + 2
    while time.monotonic() < deadline:
        with session_factory()() as db:
            command = db.get(ProbeCommand, command_id)
            if command.status == "succeeded":
                result = unseal(command.result_cipher)
                return result
            if command.status in TERMINAL:
                code = command.error_code or "probe_operation_failed"
                if code in {
                    "device_paused",
                    "chrome_closed",
                    "probe_response_timeout",
                    "command_expired",
                }:
                    code = "waiting_device"
                raise CollectionError(code, "探针操作未完成：" + code)
        time.sleep(0.2)
    with session_factory()() as db:
        command = db.scalar(
            select(ProbeCommand).where(ProbeCommand.id == command_id).with_for_update()
        )
        if command.status == "succeeded":
            result = unseal(command.result_cipher)
            return result
        if command.status not in TERMINAL:
            command.status = "unknown" if command.status == "sent" else "expired"
            command.error_code = (
                "execution_unknown" if command.status == "unknown" else "waiting_device"
            )
            command.finished_at = now()
            db.commit()
        raise CollectionError(
            command.error_code or "waiting_device", "探针响应超时；未自动重放操作"
        )


class ProbeBrowser:
    def __init__(self, account_id, frozen, task_id, attempt):
        with session_factory()() as db:
            self.session_id = acquire_session(
                db, account_id, frozen=frozen, task_id=task_id, attempt=attempt
            ).id
        self.closed = False

    def call(self, operation, payload=None):
        with session_factory()() as db:
            session = db.get(ProbeBrowserSession, self.session_id)
            command = enqueue(db, session, operation, payload)
        return wait_result(command.id)

    def start(self, response_path_pattern=None):
        return self.call("browser.start_task", {"response_path_pattern": response_path_pattern})

    def navigate(self, url):
        return self.call("browser.navigate", {"url": url})

    def read(self):
        return self.call("browser.read_page")

    def click(self, selector):
        return self.call("browser.click", {"selector": selector})

    def scroll(self):
        return self.call("browser.scroll", {"direction": "down"})

    def close(self):
        if self.closed:
            return
        self.closed = True
        with session_factory()() as db:
            session = db.scalar(
                select(ProbeBrowserSession)
                .where(ProbeBrowserSession.id == self.session_id)
                .with_for_update()
            )
            if session and session.status == "active":
                try:
                    enqueue(db, session, "browser.close_session")
                except CollectionError:
                    pass
                session.status = "finished"
                db.commit()
