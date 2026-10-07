import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import User, UserSession, now

hasher = PasswordHasher()
Db = Annotated[Session, Depends(get_db)]


def bootstrap_admin(db: Session):
    settings = get_settings()
    if db.scalar(select(User).where(User.username == settings.admin_username)):
        return
    password = settings.admin_password.get_secret_value()
    if len(password) < 12:
        raise RuntimeError("首次启动需要至少 12 位的 ADMIN_PASSWORD，请保存在私有环境文件中。")
    db.add(User(username=settings.admin_username, password_hash=hasher.hash(password)))
    db.commit()


def verify_password(value: str, digest: str) -> bool:
    try:
        return hasher.verify(digest, value)
    except VerificationError:
        return False


def new_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(40)
    db.add(UserSession(
        user_id=user.id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=now() + timedelta(hours=get_settings().session_hours),
    ))
    # Expiration blocks authentication immediately; physical cleanup follows
    # the configured, audited retention policy instead of another user's login.
    db.commit()
    return token


def current_user(request: Request, db: Db) -> User:
    token = request.cookies.get("hr_session", "")
    if not token:
        raise HTTPException(401, "请先登录")
    session = db.scalar(select(UserSession).where(
        UserSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
        UserSession.expires_at > now(),
    ))
    user = db.get(User, session.user_id) if session else None
    if not user or not user.active:
        raise HTTPException(401, "登录已失效")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


AdminUser = Annotated[User, Depends(admin_user)]
