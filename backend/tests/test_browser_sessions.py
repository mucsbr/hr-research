from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import uuid

from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError
import pytest

from app.browser_api import LoginAction
from app.browser_sessions import HumanLogin, load_state, login_manager, save_state, state_file
from app.config import get_settings


@pytest.fixture
def isolated_state(db, tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_profiles_dir", tmp_path / "accounts")
    monkeypatch.setattr(get_settings(), "encryption_key", SecretStr(Fernet.generate_key().decode()))


def test_authentication_state_is_encrypted_at_rest(isolated_state):
    account = uuid.uuid4()
    payload = {"cookies": [{"name": "session", "value": "private-cookie-value"}], "origins": []}
    save_state(account, payload)
    assert b"private-cookie-value" not in state_file(account).read_bytes()
    assert load_state(account) == payload
    assert state_file(account).stat().st_mode & 0o777 == 0o600


def test_input_is_not_stripped_and_arbitrary_commands_are_rejected():
    action = LoginAction(action="type", text=" keep spaces ")
    assert action.text == " keep spaces "
    with pytest.raises(ValidationError):
        LoginAction(action="click", x=1300, y=20)
    with pytest.raises(ValidationError):
        LoginAction(action="press", key="Control+L")
    with pytest.raises(ValidationError):
        LoginAction(action="evaluate", text="arbitrary code")


def test_human_frame_input_and_save_without_external_login(isolated_state):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = """<!doctype html><html><body>
            <input id="name" style="position:absolute;left:20px;top:20px;width:250px;height:40px">
            <button style="position:absolute;left:20px;top:90px;width:150px;height:45px"
            onclick="localStorage.setItem('test-name',document.getElementById('name').value);document.cookie='demo=auth;path=/'">
            Local test login</button></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    account_id = uuid.uuid4()
    session = HumanLogin(uuid.uuid4(), account_id, "boss", uuid.uuid4(),
                         test_url=f"http://127.0.0.1:{server.server_port}/")
    try:
        session.start()
        assert session.status == "active"
        assert session.call("frame").startswith(b"\x89PNG")
        session.call("click", {"x": 60, "y": 40})
        session.call("type", {"text": " local test "})
        session.call("click", {"x": 60, "y": 110})
        response = session.call("save")
        session.thread.join(timeout=5)
        assert response["saved"] and response["verified"] is False
        state = load_state(account_id)
        assert any(cookie["name"] == "demo" for cookie in state["cookies"])
        assert state["origins"][0]["localStorage"][0]["value"] == " local test "
        assert session.closed
    finally:
        if not session.closed:
            session.call("close")
            session.thread.join(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_session_status_does_not_expose_other_users_session(signed_client):
    from types import SimpleNamespace
    identifier = uuid.uuid4()
    login_manager.sessions[identifier] = SimpleNamespace(
        id=identifier, owner=uuid.uuid4(), closed=True
    )
    try:
        response = signed_client.get(f"/api/v1/login-sessions/{identifier}")
        assert response.status_code == 404
    finally:
        login_manager.sessions.pop(identifier)
