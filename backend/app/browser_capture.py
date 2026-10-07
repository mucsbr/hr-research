"""Anonymous, short-lived official-site rendering; never uses a personal browser profile."""
import os
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from app.collection import CollectionError, validate_fetch_url
from app.config import get_settings


class BrowserCapture:
    def __init__(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_settings().browser_cache_dir)
        self.playwright = sync_playwright().start()
        self.known_hosts: dict[str, str] = {}
        self.navigation_guard = None
        self.check = None

    def close(self):
        self.playwright.stop()

    def fetch(self, url: str):
        if self.check:
            self.check()
        if self.navigation_guard:
            self.navigation_guard(url)
        host = urlparse(url).hostname
        self.known_hosts[host] = validate_fetch_url(url)
        for _ in range(6):
            if self.check:
                self.check()
            blocked_hosts = set()
            boundary_error = None
            rules = ", ".join("MAP " + hostname + " " + ip
                              for hostname, ip in self.known_hosts.items())
            browser = self.playwright.chromium.launch(
                channel="chromium", headless=True,
                args=["--host-resolver-rules=" + rules, "--no-proxy-server"],
            )
            context = browser.new_context(
                locale="zh-CN", service_workers="block", accept_downloads=False,
            )
            page = context.new_page()

            def route_request(route):
                nonlocal boundary_error
                request = route.request
                parsed = urlparse(request.url)
                try:
                    if self.check:
                        self.check()
                    if (self.navigation_guard and request.is_navigation_request()
                            and request.frame == request.frame.page.main_frame):
                        self.navigation_guard(request.url)
                except CollectionError as exc:
                    boundary_error = exc
                    route.abort()
                    return
                if parsed.scheme not in {"http", "https"}:
                    route.abort()
                    return
                if request.resource_type in {"media", "font", "image"}:
                    route.abort()
                    return
                if any(name in (parsed.hostname or "") for name in (
                    "google-analytics", "googletagmanager", "doubleclick", "hm.baidu", "umeng",
                )):
                    route.abort()
                    return
                if parsed.hostname not in self.known_hosts:
                    blocked_hosts.add(parsed.hostname)
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", route_request)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                if response and response.status == 401:
                    raise CollectionError("waiting_login", "官网要求有效登录，未读取受限页面")
                if response and response.status in {403, 429}:
                    raise CollectionError("access_restricted", "官网限制访问，未将错误页作为招聘内容")
                if response and response.status >= 400:
                    raise CollectionError("http_error", f"官网返回 HTTP {response.status}")
                try:
                    page.wait_for_load_state("networkidle", timeout=2500)
                except PlaywrightError:
                    pass
                final_url, html = page.url, page.content()
                if len(html) > 3_000_000:
                    raise CollectionError("document_too_large", "浏览器页面超过内容预算")
            except PlaywrightError as exc:
                if boundary_error:
                    raise boundary_error
                if not blocked_hosts:
                    raise CollectionError("browser_navigation", "浏览器页面无法完成加载") from exc
                final_url, html = url, ""
            finally:
                context.close()
                browser.close()
            if boundary_error:
                raise boundary_error
            if self.check:
                self.check()
            if self.navigation_guard:
                self.navigation_guard(final_url)
            if not blocked_hosts:
                validate_fetch_url(final_url)
                return final_url, html
            if len(self.known_hosts) + len(blocked_hosts) > 12:
                raise CollectionError("browser_host_budget", "页面依赖域名超过本次浏览器预算")
            for hostname in sorted(blocked_hosts):
                # Resolve before relaunch, then pin every permitted hostname in Chromium.
                self.known_hosts[hostname] = validate_fetch_url("https://" + hostname)
        raise CollectionError("browser_host_budget", "页面仍需额外来源，未继续扩张浏览器范围")
