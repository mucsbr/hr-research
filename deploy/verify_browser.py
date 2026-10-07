"""Exercise container Chromium and remote-login controls against a loopback fixture only.

Run with `docker compose ... exec -T api python - < deploy/verify_browser.py`.
This overrides settings only in this short-lived process, never in the API service.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
import uuid

os.environ["APP_ENV"] = "test"

from app.browser_sessions import HumanLogin, load_state, state_file  # noqa: E402
from app.config import get_settings  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<!doctype html><html><body>
        <input style="position:absolute;left:20px;top:20px;width:250px;height:40px" id="name">
        <button style="position:absolute;left:20px;top:90px;width:150px;height:45px"
        onclick="localStorage.setItem('fixture-name',document.getElementById('name').value);
        document.cookie='fixture-session=deployment-cookie;path=/'">Save fixture</button>
        </body></html>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    assert os.getuid() != 0, "Browser verification must run as the application user"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    account_id = uuid.uuid4()
    session = HumanLogin(uuid.uuid4(), account_id, "boss", uuid.uuid4(),
                         test_url=f"http://127.0.0.1:{server.server_port}/")
    try:
        session.start()
        frame = session.call("frame")
        assert frame.startswith(b"\x89PNG")
        session.call("click", {"x": 60, "y": 40})
        session.call("type", {"text": " container fixture "})
        session.call("click", {"x": 60, "y": 110})
        saved = session.call("save")
        session.thread.join(timeout=5)
        assert saved["saved"] and saved["verified"] is False
        state = load_state(account_id)
        assert any(cookie["name"] == "fixture-session" for cookie in state["cookies"])
        assert state["origins"][0]["localStorage"][0]["value"] == " container fixture "
        encrypted = state_file(account_id)
        assert b"deployment-cookie" not in encrypted.read_bytes()
        assert encrypted.stat().st_mode & 0o777 == 0o600
        result = {"chromium_channel": "chromium", "non_root_uid": os.getuid(),
                  "png_bytes": len(frame), "frame_click_type_save": True,
                  "encrypted_session": True, "session_file_mode": "0600",
                  "account_id": str(account_id), "real_platform_login": False}
        # Keep only fixture state and a non-secret marker to check the named volume after restart.
        marker = Path(get_settings().storage_dir) / "deployment-browser-verification.json"
        marker.write_text(json.dumps(result, indent=2))
        print(json.dumps(result, indent=2))
    finally:
        if not session.closed:
            session.call("close")
            session.thread.join(timeout=5)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
