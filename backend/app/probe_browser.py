"""Small synchronous Playwright-shaped facade; all browser actions execute on the probe.

It deliberately cannot export cookies or close the externally managed Chrome context.
Existing verified parsers and remote program isolation remain on the server.
"""

import json
import time
import uuid

from app.collection import CollectionError
from app.probe_service import ProbeBrowser


class ProbeResponse:
    def __init__(self, value):
        self.url = value.get("url", "")
        self.status = value.get("status", 200)
        self.headers = {"content-type": value.get("content_type", "application/json")}
        self.value = value.get("body")

    def body(self):
        return json.dumps(self.value, ensure_ascii=False).encode()

    def json(self):
        return self.value


class ProbeLocator:
    def __init__(self, page, selector, nth=0):
        self.page, self.selector, self.nth = page, selector, nth

    @property
    def first(self):
        return ProbeLocator(self.page, self.selector, 0)

    @property
    def last(self):
        return ProbeLocator(self.page, self.selector, -1)

    def _inspect(self):
        return self.page._call("browser.locate", {"selector": self.selector, "nth": self.nth})

    def count(self):
        return self._inspect()["count"]

    def inner_text(self, timeout=None):
        return self._inspect().get("text", "")

    def is_visible(self):
        return self._inspect().get("visible", False)

    def is_enabled(self):
        return not self.is_disabled()

    def is_disabled(self):
        return self._inspect().get("disabled", True)

    def get_attribute(self, name, timeout=None):
        if name not in {"disabled", "aria-disabled", "class", "href"}:
            raise CollectionError("probe_operation_forbidden", "探针不读取任意表单字段")
        return self._inspect().get("attributes", {}).get(name)

    def click(self, timeout=None):
        value = self.page._call("browser.click", {"selector": self.selector, "nth": self.nth})
        self.page._snapshot(value)


class ProbePage:
    def __init__(self, context):
        self.context, self.id = context, str(uuid.uuid4())
        self.url, self.html, self.text = "about:blank", "", ""
        self.callbacks = []
        self.mouse = ProbeMouse(self)
        context.remote.call(
            "browser.start_task",
            {"page_id": self.id, "response_path_pattern": context.response_pattern},
        )

    def _call(self, operation, payload=None):
        return self.context.remote.call(operation, {"page_id": self.id, **(payload or {})})

    def _snapshot(self, value):
        if "url" in value:
            self.url = value["url"]
        if "html" in value:
            self.html, self.text = value["html"], value.get("text", "")
        for item in value.get("responses", []):
            for callback in self.callbacks:
                callback(ProbeResponse(item))
        return value

    def goto(self, url, **kwargs):
        value = self._snapshot(self._call("browser.navigate", {"url": url}))
        return ProbeResponse({"url": self.url, "status": value.get("status", 200)})

    def on(self, name, callback):
        if name != "response":
            raise CollectionError("probe_operation_forbidden", "该探针事件未支持")
        self.callbacks.append(callback)

    def set_default_timeout(self, timeout):
        pass

    def wait_for_load_state(self, state, timeout=None):
        self._snapshot(self._call("browser.read_page"))

    def wait_for_timeout(self, milliseconds):
        time.sleep(min(milliseconds / 1000, 2))

    def content(self):
        self._snapshot(self._call("browser.read_page"))
        return self.html

    def locator(self, selector):
        return ProbeLocator(self, selector)


class ProbeMouse:
    def __init__(self, page):
        self.page = page

    def wheel(self, x, y):
        self.page._snapshot(
            self.page._call("browser.scroll", {"direction": "up" if y < 0 else "down"})
        )


class ProbeContext:
    def __init__(self, remote, response_pattern):
        self.remote, self.response_pattern = remote, response_pattern

    def new_page(self):
        return ProbePage(self)

    def route(self, pattern, callback):
        # The probe enforces the session's platform URLs on its task-owned pages.
        # Never install a context-global interceptor into the user's real Chrome.
        pass

    def storage_state(self, **kwargs):
        raise CollectionError("probe_operation_forbidden", "探针登录状态不能导出到服务器")

    def close(self):
        self.remote.close()


class ProbePlaywright:
    def __init__(self, account_id, frozen, runtime_scope, response_pattern):
        if not runtime_scope or not runtime_scope.get("task_id"):
            raise CollectionError("blocked_configuration", "探针采集必须关联实际任务执行")
        self.remote = ProbeBrowser(
            account_id, frozen, runtime_scope["task_id"], runtime_scope["expected_attempt"]
        )
        self.context = ProbeContext(self.remote, response_pattern)
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.remote.close()

    def launch(self, **kwargs):
        return self

    def new_context(self, **kwargs):
        # Reuses the local Chrome default context, not the supplied storage_state/viewport.
        return self.context

    def close(self):
        self.remote.close()


class ProbeSentimentFetcher:
    def __init__(self, account_id, frozen, task_id, attempt):
        self.remote = ProbeBrowser(account_id, frozen, task_id, attempt)
        self.context = ProbeContext(self.remote, None)
        self.page = self.context.new_page()

    def fetch(self, url):
        from app.collection import require_readable_page

        self.page.goto(url)
        value = self.page._snapshot(self.page._call("browser.read_page"))
        status = value.get("status", 200)
        if status == 401:
            raise CollectionError("waiting_login", "Chrome中的来源要求登录")
        if status in {403, 429}:
            raise CollectionError("waiting_verification", "Chrome中的来源要求人工验证")
        if status >= 400:
            raise CollectionError("http_error", f"来源返回HTTP {status}")
        require_readable_page(self.page.html)
        return self.page.url, self.page.html

    def close(self):
        self.remote.close()
