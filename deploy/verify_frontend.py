"""Render the deployed React login screen through Nginx, with all external requests blocked."""
import json
import argparse
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from app.config import get_settings

parser = argparse.ArgumentParser()
parser.add_argument("--authenticated", action="store_true")
args = parser.parse_args()

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel="chromium", headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    context.route("**/*", lambda route: route.continue_()
                  if urlparse(route.request.url).netloc == "web:8080" else route.abort())
    settings = get_settings()
    if args.authenticated:
        login = context.request.post("http://web:8080/api/v1/auth/login", data={
            "username": settings.admin_username, "password": settings.admin_password.get_secret_value()})
        assert login.status == 200
    page = context.new_page()
    errors = []
    failed_requests = []
    compatibility_warnings = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: compatibility_warnings.append(message.text)
            if message.type in {"warning", "error"} and "compatible" in message.text.lower()
            and "antd" in message.text.lower() else None)
    page.on("response", lambda response: failed_requests.append({
        "path": urlparse(response.url).path, "status": response.status})
        if response.status >= 400 and "/api/" in response.url else None)
    if args.authenticated:
        routes = ["/", "/companies", "/jobs", "/sources", "/analytics", "/sentiment", "/news",
                  "/plans", "/tasks", "/reports", "/assistant", "/notifications", "/maintenance", "/settings"]
        output = Path(settings.storage_dir) / "deployment-pages"
        output.mkdir(exist_ok=True)
        pages = []
        for route in routes:
            response = page.goto("http://web:8080" + route, wait_until="networkidle", timeout=20000)
            assert response.status == 200
            page.locator("h1").wait_for(timeout=10000)
            assert page.locator(".load-error").count() == 0, route
            if route == "/":
                recent_names = page.locator(".activity-row .grow strong").all_inner_texts()
                assert recent_names and all(name.strip() for name in recent_names), "Recent tasks need visible names"
                assert "单公司独立任务" not in page.locator("body").inner_text()
            pages.append({"route": route, "title": page.locator("h1").first.inner_text()})
            page.screenshot(path=str(output / ((route.strip("/") or "overview") + ".png")), full_page=True)
        assert not errors and not failed_requests and not compatibility_warnings, (
            errors, failed_requests, compatibility_warnings)
        context.request.post("http://web:8080/api/v1/auth/logout", data={})
        result = {"authenticated_pages": pages, "js_errors": errors, "api_errors": failed_requests,
                  "antd_compatibility_warnings": compatibility_warnings,
                  "external_requests_allowed": False}
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        browser.close()
        raise SystemExit(0)
    response = page.goto("http://web:8080", wait_until="networkidle", timeout=20000)
    assert response.status == 200
    page.locator('input[type="password"]').wait_for(timeout=10000)
    assert page.get_by_role("button", name="登 录").count() or page.get_by_role("button", name="登录").count()
    assert not errors, errors
    screenshot = Path(get_settings().storage_dir) / "deployment-login-screen.png"
    page.screenshot(path=str(screenshot), full_page=True)
    print(json.dumps({"react_rendered": True, "login_form_visible": True, "js_errors": errors,
                      "screenshot": str(screenshot), "external_requests_allowed": False}))
    browser.close()
