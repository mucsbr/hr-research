"""Read-only live Web checks. Execute in an isolated remote browser container."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/app/backend")


def main():
    from app.config import get_settings
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/playwright"
    errors, writes, responses = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on("request", lambda request: writes.append({"method": request.method, "path": request.url.split("/api/v1")[-1]})
                if "/api/v1/" in request.url and request.method not in {"GET", "HEAD", "OPTIONS"}
                and "/auth/" not in request.url else None)
        page.on("response", lambda response: responses.append({"path": response.url.split("/api/v1")[-1], "status": response.status})
                if "/api/v1/" in response.url and response.status >= 400 and "/auth/me" not in response.url else None)
        page.goto(args.base + "/devices")
        page.get_by_label("用户名", exact=True).fill(settings.admin_username)
        page.get_by_label("密码", exact=True).fill(settings.admin_password.get_secret_value())
        page.get_by_role("button", name="登录工作台").click()
        page.get_by_role("link", name="采集设备", exact=True).click()
        expect(page.get_by_role("heading", name="采集设备", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name="下载安装包")).to_have_count(3)
        expect(page.get_by_text("Windows · x64", exact=True)).to_be_visible()
        expect(page.get_by_text("macOS · arm64", exact=True)).to_be_visible()
        page.get_by_role("button", name="新增独立浏览器资料", exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_role("dialog").get_by_role("button", name="Close", exact=True).click()
        expect(page.get_by_role("dialog")).not_to_be_visible()
        page.screenshot(path=str(args.output / "devices.png"), full_page=True)
        page.get_by_role("link", name="数据源", exact=True).click()
        expect(page.get_by_role("heading", name="数据源与登录", exact=True)).to_be_visible()
        expect(page.get_by_text("招聘信息浏览账号", exact=True)).to_be_visible()
        expect(page.get_by_text("采集浏览器", exact=True)).to_be_visible()
        page.screenshot(path=str(args.output / "source-accounts.png"), full_page=True)
        width = page.evaluate("({viewport:innerWidth,body:document.body.scrollWidth})")
        assert width["body"] <= width["viewport"], "Page overflow"
        assert not errors and not writes and not responses, "UI errors or business writes"
        context.request.post(args.base + "/api/v1/auth/logout", data={})
        browser.close()
    summary = {"status": "passed", "javascript_errors": errors, "business_writes": writes,
               "api_errors": responses, "width": width, "pages": ["devices", "source_accounts"],
               "native_probe": False, "model_requests": 0}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
