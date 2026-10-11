import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Response
from pydantic import ConfigDict, Field, model_validator

from app.browser_sessions import BrowserSessionError, VIEWPORT, login_manager
from app.models import SourceAccount
from app.schemas import Input
from app.security import AdminUser, Db

router = APIRouter(prefix="/api/v1")


class LoginAction(Input):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    action: Literal["click", "drag", "type", "press", "scroll", "back"]
    x: float | None = Field(default=None, ge=0, lt=1280)
    y: float | None = Field(default=None, ge=0, lt=820)
    to_x: float | None = Field(default=None, ge=0, lt=1280)
    to_y: float | None = Field(default=None, ge=0, lt=820)
    text: str | None = Field(default=None, max_length=500)
    key: Literal["Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp",
                 "Control+A", "Meta+A"] | None = None
    delta: int | None = Field(default=None, ge=-800, le=800)

    @model_validator(mode="after")
    def required_fields(self):
        fields = {"click": ["x", "y"], "drag": ["x", "y", "to_x", "to_y"],
                  "type": ["text"], "press": ["key"], "scroll": ["delta"], "back": []}
        if any(getattr(self, field) is None for field in fields[self.action]):
            raise ValueError("操作参数不完整")
        return self


def translate(exc):
    status = 404 if exc.code == "session_missing" else 410 if exc.code == "session_closed" else 409
    return HTTPException(status, exc.message)


def visible(session):
    return {"id": str(session.id), "account_id": str(session.account_id),
            "status": session.status, "url": session.safe_url, "title": session.page_title,
            "viewport": VIEWPORT}


@router.post("/source-accounts/{account_id}/login-sessions")
def start_login(account_id: uuid.UUID, db: Db, user: AdminUser):
    account = db.get(SourceAccount, account_id)
    if not account:
        raise HTTPException(404, "账号不存在")
    from app.probe_service import snapshot_for_account
    if snapshot_for_account(db, account_id):
        raise HTTPException(409, "该账号使用桌面探针，请在绑定设备的Chrome中登录")
    try:
        return visible(login_manager.start(account_id, account.kind, user.id))
    except BrowserSessionError as exc:
        raise translate(exc) from None


@router.get("/login-sessions/{session_id}")
def login_status(session_id: uuid.UUID, user: AdminUser):
    try:
        return visible(login_manager.get(session_id, user.id))
    except BrowserSessionError as exc:
        raise translate(exc) from None


@router.get("/login-sessions/{session_id}/frame")
def login_frame(session_id: uuid.UUID, user: AdminUser):
    try:
        frame = login_manager.get(session_id, user.id).call("frame")
        return Response(frame, media_type="image/png", headers={"Cache-Control": "no-store"})
    except BrowserSessionError as exc:
        raise translate(exc) from None


@router.post("/login-sessions/{session_id}/actions")
def login_action(session_id: uuid.UUID, data: LoginAction, user: AdminUser):
    try:
        return login_manager.get(session_id, user.id).call(
            data.action, data.model_dump(exclude_none=True)
        )
    except BrowserSessionError as exc:
        raise translate(exc) from None


@router.post("/login-sessions/{session_id}/save")
def save_login(session_id: uuid.UUID, user: AdminUser):
    try:
        session = login_manager.get(session_id, user.id)
        result = session.call("save")
        session.thread.join(timeout=5)
        login_manager.release(session_id)
        return result
    except BrowserSessionError as exc:
        raise translate(exc) from None


@router.post("/login-sessions/{session_id}/close")
def close_login(session_id: uuid.UUID, user: AdminUser):
    try:
        session = login_manager.get(session_id, user.id)
        if not session.closed:
            session.call("close")
            session.thread.join(timeout=5)
        login_manager.release(session_id)
        return {"closed": True}
    except BrowserSessionError as exc:
        raise translate(exc) from None
