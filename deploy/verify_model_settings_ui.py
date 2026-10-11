"""Live or isolated UI checks. Writes require an explicit *_test database environment."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, "/app/backend")


def main():
    from app.config import get_settings
    from playwright.sync_api import expect, sync_playwright
    from sqlalchemy.engine import make_url

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qa-writes", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    if args.qa_writes:
        assert settings.app_env == "test" and make_url(settings.database_url.get_secret_value()).database.endswith("_test")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/playwright"
    args.output.mkdir(parents=True, exist_ok=True)
    errors, writes, rejected = [], [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on("request", lambda r: writes.append(r.url.split("/api/v1")[-1]) if "/api/v1/" in r.url and r.method not in {"GET", "HEAD", "OPTIONS"} and "/auth/" not in r.url else None)
        page.on("response", lambda r: rejected.append({"path": r.url.split("/api/v1")[-1], "status": r.status}) if "/api/v1/" in r.url and r.status >= 400 and "/auth/me" not in r.url else None)
        for _ in range(60):
            if context.request.get(args.base + "/api/v1/readiness").status == 200:
                break
            time.sleep(0.2)
        else:
            raise AssertionError("API startup did not become ready")
        page.goto(args.base + "/settings")
        page.get_by_label("用户名", exact=True).fill(settings.admin_username)
        page.get_by_label("密码", exact=True).fill(settings.admin_password.get_secret_value())
        page.get_by_role("button", name="登录工作台").click()
        page.get_by_role("link", name="运行设置", exact=True).click()
        before = context.request.get(args.base + "/api/v1/system/status").json()["llm"]["calls_today"]
        page.get_by_role("button", name="配置模型", exact=True).click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_label("Base URL", exact=True)).to_be_visible()
        expect(dialog.get_by_label("API Key", exact=True)).to_have_value("")
        expect(dialog.get_by_label("模型名", exact=True)).to_be_visible()
        if args.qa_writes:
            dialog.get_by_label("Base URL", exact=True).fill("https://compatible.example.invalid/v1")
            dialog.get_by_label("API Key", exact=True).fill("model-ui-sample-no-actual-key")
            dialog.get_by_label("模型名", exact=True).fill("custom-deployment/any-model:2026")
            enabled = dialog.locator("#enabled")
            if enabled.get_attribute("aria-checked") == "false":
                enabled.click()
            dialog.get_by_label("每日请求上限", exact=True).fill("1")
            dialog.get_by_role("button", name="保存配置", exact=True).click()
            expect(dialog).not_to_be_visible()
            expect(page.get_by_text("custom-deployment/any-model:2026", exact=True)).to_be_visible()
            page.get_by_role("button", name="配置模型", exact=True).click()
            expect(dialog.get_by_label("API Key", exact=True)).to_have_value("")
            dialog.get_by_label("模型名", exact=True).fill("second-free-model")
            dialog.get_by_role("button", name="保存配置", exact=True).click()
            expect(dialog).not_to_be_visible()
            page.get_by_role("button", name="配置模型", exact=True).click()
            dialog.get_by_label("Base URL", exact=True).fill("https://new-provider.example.invalid/v1")
            dialog.get_by_role("button", name="保存配置", exact=True).click()
            expect(page.get_by_text("更换Base URL时请填写该服务的API Key，或先清除原密钥", exact=True)).to_be_visible()
            expect(dialog).to_be_visible()
        dialog.get_by_role("button", name="Close", exact=True).click()
        expect(dialog).not_to_be_visible()
        saved = context.request.get(args.base + "/api/v1/settings/models").json()
        assert "api_key" not in saved and "encrypted_api_key" not in saved
        after = context.request.get(args.base + "/api/v1/system/status").json()["llm"]["calls_today"]
        assert after == before, "Configuration changed the call ledger"
        if args.qa_writes:
            assert saved["model"] == "second-free-model" and saved["has_api_key"]
            assert saved["base_url"] == "https://compatible.example.invalid/v1"
            assert len(writes) == 3 and rejected == [{"path": "/settings/models", "status": 422}]
        else:
            assert not writes and not rejected
        assert not errors
        width = page.evaluate("({viewport:innerWidth,body:document.body.scrollWidth})")
        assert width["body"] <= width["viewport"]
        page.screenshot(path=str(args.output / "model-settings.png"), full_page=True)
        context.request.post(args.base + "/api/v1/auth/logout", data={})
        browser.close()
    summary = {"status": "passed", "qa_writes": args.qa_writes, "business_writes": len(writes),
               "javascript_errors": errors, "expected_rejections": rejected, "width": width,
               "model_requests": 0, "call_ledger_unchanged": True}
    (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
