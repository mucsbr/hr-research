"""Account-isolated human login. Frames and input are never sent to a model or written to logs."""
from concurrent.futures import Future
from datetime import timedelta
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
from threading import RLock, Thread
import time
from urllib.parse import urlparse
import uuid

from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright
import redis

from app.config import get_settings
from app.collection import CollectionError, validate_fetch_url
from app.db import session_factory
from app.models import BrowserLoginSession, SourceAccount, now

PLATFORMS = {
    "boss": ("https://www.zhipin.com/", ("zhipin.com", "bosszhipin.com", "kanzhun.com")),
    "liepin": ("https://www.liepin.com/", ("liepin.com", "liepin.cn", "lietou-static.com")),
    "zhaopin": ("https://www.zhaopin.com/", ("zhaopin.com", "zhaopin.cn")),
    "51job": ("https://www.51job.com/", ("51job.com", "51jobcdn.com")),
    "xhs": ("https://www.xiaohongshu.com/explore", ("xiaohongshu.com", "xhscdn.com")),
    "dy": ("https://www.douyin.com/", ("douyin.com", "douyincdn.com", "bytedance.com")),
    "wb": ("https://weibo.com/", ("weibo.com", "weibo.cn", "sina.com.cn")),
}
VIEWPORT = {"width": 1280, "height": 820}


class BrowserSessionError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def state_file(account_id: uuid.UUID) -> Path:
    folder = get_settings().browser_profiles_dir
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder / (str(account_id) + ".state.enc")


def cipher():
    key = get_settings().encryption_key.get_secret_value()
    if not key:
        raise BrowserSessionError("encryption_not_configured", "请先配置会话加密密钥")
    try:
        return Fernet(key.encode())
    except ValueError:
        raise BrowserSessionError("encryption_not_configured", "会话加密密钥格式不正确") from None


def load_state(account_id):
    path = state_file(account_id)
    if not path.exists():
        return None
    try:
        return json.loads(cipher().decrypt(path.read_bytes()))
    except Exception:
        raise BrowserSessionError("state_invalid", "已保存会话无法解密，需要重新建立登录会话") from None


def save_state(account_id, state):
    path = state_file(account_id)
    encrypted = cipher().encrypt(json.dumps(state).encode())
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encrypted)
    os.replace(temporary, path)
    path.chmod(0o600)


class HumanLogin:
    def __init__(self, session_id, account_id, kind, owner, *, test_url=None, test_state=None):
        if kind not in PLATFORMS:
            raise BrowserSessionError("unsupported_source", "这个来源尚未接入登录适配")
        self.id, self.account_id, self.kind, self.owner = session_id, account_id, kind, owner
        self.url = test_url or PLATFORMS[kind][0]
        if test_url and get_settings().app_env != "test":
            raise BrowserSessionError("invalid_login_url", "不允许替换登录目标")
        self.test_url, self.test_state = test_url, test_state
        self.queue: Queue = Queue(maxsize=20)
        self.start_result: Future = Future()
        self.deadline = time.monotonic() + get_settings().browser_login_seconds
        self.closed = False
        self.status = "starting"
        self.page_title = ""
        self.safe_url = self.url
        self.thread = Thread(target=self._run, daemon=True, name="login-" + str(session_id)[:8])

    def start(self):
        self.thread.start()
        try:
            self.start_result.result(timeout=30)
        except TimeoutError:
            raise BrowserSessionError("browser_starting", "浏览器启动较慢，请稍后查看会话") from None

    def call(self, action, payload=None):
        if self.closed or not self.thread.is_alive():
            raise BrowserSessionError("session_closed", "登录会话已结束")
        future = Future()
        try:
            self.queue.put_nowait((action, payload or {}, future))
        except Full:
            raise BrowserSessionError("session_busy", "登录会话正在处理操作，请稍后重试") from None
        try:
            return future.result(timeout=15)
        except TimeoutError:
            raise BrowserSessionError("action_timeout", "页面操作超时，请查看当前画面") from None

    def _allowed_navigation(self, url):
        parsed = urlparse(url)
        if self.test_url:
            return parsed.netloc == urlparse(self.test_url).netloc
        if parsed.scheme != "https" or parsed.username or parsed.password:
            return False
        host = parsed.hostname or ""
        suffixes = PLATFORMS[self.kind][1]
        return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)

    def _run(self):
        context = browser = playwright = None
        terminal = "closed"
        try:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_settings().browser_cache_dir)
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(channel="chromium", headless=True)
            state = self.test_state if self.test_url else load_state(self.account_id)
            context = browser.new_context(
                viewport=VIEWPORT, locale="zh-CN", timezone_id="Asia/Shanghai", service_workers="block",
                accept_downloads=False, storage_state=state,
            )
            page = context.new_page()
            page.set_default_timeout(8000)

            def guard(route):
                request = route.request
                parsed = urlparse(request.url)
                if parsed.scheme not in {"http", "https"}:
                    route.abort()
                elif self.test_url and parsed.netloc != urlparse(self.test_url).netloc:
                    route.abort()
                else:
                    if not self.test_url:
                        try:
                            validate_fetch_url(request.url)
                        except CollectionError:
                            route.abort()
                            return
                    if request.is_navigation_request() and request.frame == request.frame.page.main_frame and not self._allowed_navigation(request.url):
                        route.abort()
                        return
                    route.continue_()

            context.route("**/*", guard)
            page.goto(self.url, wait_until="domcontentloaded", timeout=25000)
            self.status = "active"
            self.start_result.set_result(True)
            while time.monotonic() < self.deadline:
                parsed = urlparse(page.url)
                self.safe_url = parsed.scheme + "://" + parsed.netloc + parsed.path
                self.page_title = page.title()
                try:
                    action, payload, future = self.queue.get(timeout=0.25)
                except Empty:
                    page.wait_for_timeout(100)
                    continue
                try:
                    if action == "frame":
                        result = page.screenshot(type="png", animations="disabled")
                    elif action == "click":
                        page.mouse.click(payload["x"], payload["y"])
                        result = {"ok": True}
                    elif action == "drag":
                        page.mouse.move(payload["x"], payload["y"])
                        page.mouse.down()
                        page.mouse.move(payload["to_x"], payload["to_y"], steps=12)
                        page.mouse.up()
                        result = {"ok": True}
                    elif action == "type":
                        page.keyboard.insert_text(payload["text"])
                        result = {"ok": True}
                    elif action == "press":
                        page.keyboard.press(payload["key"])
                        result = {"ok": True}
                    elif action == "scroll":
                        page.mouse.wheel(0, payload["delta"])
                        result = {"ok": True}
                    elif action == "back":
                        page.go_back(wait_until="domcontentloaded")
                        result = {"ok": True}
                    elif action == "save":
                        state = context.storage_state(indexed_db=True)
                        save_state(self.account_id, state)
                        if not self.test_url:
                            with session_factory()() as db:
                                account = db.get(SourceAccount, self.account_id)
                                account.session_ref = str(self.account_id) + ".state.enc"
                                account.health = "saved"
                                db.commit()
                        terminal = "saved"
                        result = {"saved": True, "health": "saved", "verified": False}
                        future.set_result(result)
                        break
                    elif action == "close":
                        future.set_result({"closed": True})
                        break
                    else:
                        raise BrowserSessionError("invalid_action", "不支持此操作")
                    future.set_result(result)
                except Exception as exc:
                    error = exc if isinstance(exc, BrowserSessionError) else BrowserSessionError(
                        "browser_action_failed", "页面操作未完成，请检查当前画面"
                    )
                    future.set_exception(error)
            else:
                terminal = "expired"
        except Exception as exc:
            terminal = "failed"
            safe_error = exc if isinstance(exc, BrowserSessionError) else BrowserSessionError(
                "browser_failed", "登录页面无法启动，请检查浏览器和网络状态"
            )
            if not self.start_result.done():
                self.start_result.set_exception(safe_error)
        finally:
            self.status, self.closed = terminal, True
            for resource, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
                if resource:
                    try:
                        getattr(resource, method)()
                    except Exception:
                        pass
            while not self.queue.empty():
                _, _, future = self.queue.get_nowait()
                if not future.done():
                    future.set_exception(BrowserSessionError("session_closed", "登录会话已结束"))
            if not self.test_url:
                with session_factory()() as db:
                    row = db.get(BrowserLoginSession, self.id)
                    if row:
                        row.status = terminal
                        if terminal == "failed":
                            row.error_message = "登录会话未能完成，请重试"
                        db.commit()


class LoginManager:
    def __init__(self):
        self.sessions: dict[uuid.UUID, HumanLogin] = {}
        self.leases = {}
        self.mutex = RLock()

    def start(self, account_id, kind, owner):
        with self.mutex:
            cipher()
            for identifier, old_session in list(self.sessions.items()):
                if old_session.closed:
                    self.release(identifier)
                    if len(self.sessions) >= 50:
                        self.sessions.pop(identifier, None)
            active = [session for session in self.sessions.values() if not session.closed]
            for session in active:
                if session.account_id == account_id:
                    if session.owner != owner:
                        raise BrowserSessionError("account_busy", "该账号正在由另一位用户登录")
                    return session
            if len(active) >= 3:
                raise BrowserSessionError("browser_busy", "同时登录会话已达到上限")
            client = redis.Redis.from_url(get_settings().redis_url.get_secret_value())
            lease = client.lock("account:" + str(account_id), timeout=get_settings().browser_login_seconds + 60,
                                blocking=False, thread_local=False)
            if not lease.acquire():
                raise BrowserSessionError("account_busy", "该账号正被采集任务或登录会话使用")
            try:
                with session_factory()() as db:
                    row = BrowserLoginSession(
                        account_id=account_id, owner_user_id=owner,
                        expires_at=now() + timedelta(seconds=get_settings().browser_login_seconds),
                    )
                    db.add(row)
                    db.commit()
                    identifier = row.id
                session = HumanLogin(identifier, account_id, kind, owner)
                self.sessions[identifier], self.leases[identifier] = session, lease
                session.start()
                with session_factory()() as db:
                    db.get(BrowserLoginSession, identifier).status = "active"
                    db.commit()
                return session
            except Exception:
                try:
                    lease.release()
                except redis.exceptions.LockError:
                    pass
                raise

    def get(self, identifier, owner):
        session = self.sessions.get(identifier)
        if not session or session.owner != owner:
            raise BrowserSessionError("session_missing", "会话不存在或不属于当前用户")
        if session.closed:
            self.release(identifier)
        return session

    def release(self, identifier):
        lease = self.leases.pop(identifier, None)
        if lease:
            try:
                lease.release()
            except redis.exceptions.LockError:
                pass

    def close_all(self):
        for session in list(self.sessions.values()):
            if not session.closed:
                try:
                    session.call("close")
                    session.thread.join(timeout=5)
                except BrowserSessionError:
                    pass
            self.release(session.id)


login_manager = LoginManager()
