"""Read public job responses in the platform-owned, human-authorized browser session."""
import json
import os
import re
import time
from urllib.parse import unquote, urlencode, urlparse

from playwright.sync_api import sync_playwright

from app.browser_sessions import VIEWPORT, load_state, save_state
from app.collection import (
    CollectionError, CollectionResult, JobDocument, require_readable_page, validate_fetch_url,
)
from app.config import get_settings
from app.models import now

PLATFORM_ROOTS = {"boss": "zhipin.com", "liepin": "liepin.com",
                  "zhaopin": "zhaopin.com", "51job": "51job.com"}
PUBLIC_PLATFORMS = {"zhaopin", "51job"}


def contract(kind):
    if kind not in PLATFORM_ROOTS:
        raise CollectionError("unsupported_source", "招聘来源未登记")
    from app.adapter_maintenance import active_manifest
    return active_manifest(kind)


def path_value(data, path):
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def field_value(raw, paths):
    for path in paths:
        value = path_value(raw, path)
        if value is not None and isinstance(value, (str, int, float)):
            return str(value)
    return None


def check_navigation(response):
    if response is None:
        raise CollectionError("browser_navigation", "招聘页面未返回可验证的响应")
    if response.status == 401:
        raise CollectionError("waiting_login", "招聘平台要求重新登录")
    if response.status in {403, 429}:
        raise CollectionError("waiting_verification", "招聘平台限制访问，请人工检查登录会话")
    if response.status >= 400:
        raise CollectionError("http_error", f"招聘页面返回 HTTP {response.status}")


def expected_company_id(kind, binding):
    if binding.get("external_company_id"):
        return str(binding["external_company_id"])
    patterns = {
        "boss": r"/gongsi/([^/?#.]+)", "liepin": r"/company/(\d+)",
        "zhaopin": r"^/companydetail/(?:jobs-)?([A-Za-z0-9]+)(?:\.htm|/?$)",
        "51job": r"/(?:all/)?co(\d+)\.html",
    }
    pattern = patterns.get(kind)
    if kind == "zhaopin" and urlparse(binding["entry_url"]).hostname == "company.zhaopin.com":
        pattern = r"^/([A-Za-z0-9]+)\.htm$"
    if not pattern:
        return None
    match = re.search(pattern, urlparse(binding["entry_url"]).path)
    return unquote(match.group(1)) if match else None


def same_company(raw_company, raw_id, kind, binding, identity):
    expected_id = expected_company_id(kind, binding)
    if expected_id:
        return bool(raw_id) and raw_id == expected_id
    names = [identity.get("name"), identity.get("official_name"), *identity.get("aliases", [])]
    def normalize(value):
        return re.sub(r"\s+", "", value or "").casefold()
    return bool(raw_company) and normalize(raw_company) in {normalize(name) for name in names if name}


def extract_jobs(payload, kind, binding, identity, *, mapping=None):
    mapping = contract(kind) if mapping is None else mapping
    raw_items = None
    for path in mapping["list_paths"]:
        candidate = path_value(payload, path)
        if isinstance(candidate, list) and (raw_items is None or candidate):
            raw_items = candidate
            if candidate:
                break
    if raw_items is None:
        return [], False, 0
    jobs = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        fields = {name: field_value(raw, paths) for name, paths in mapping["fields"].items()}
        if not fields["id"] or not fields["title"]:
            continue
        if not same_company(fields["company"], fields["company_id"], kind, binding, identity):
            continue
        identifier = fields["id"]
        if not re.fullmatch(r"[a-zA-Z0-9_~-]+", identifier):
            continue
        if kind == "51job":
            area = fields.get("url_area")
            if not area or not re.fullmatch(r"[a-zA-Z0-9-]+", area):
                # The public frontend supplies this path component; do not invent a city.
                continue
            url = f"https://jobs.51job.com/{area}/{identifier}.html"
        else:
            templates = {"boss": "https://www.zhipin.com/job_detail/{}.html",
                         "liepin": "https://www.liepin.com/job/{}.shtml",
                         "zhaopin": "https://www.zhaopin.com/jobdetail/{}.htm"}
            url = templates[kind].format(identifier)
        salary = fields["salary"]
        if salary and re.search(r"[\ue000-\uf8ff*]", salary):
            salary = None
        doc = JobDocument(
            source_job_id=identifier, title=fields["title"][:500], url=url,
            locations=[fields["location"]] if fields["location"] else [],
            salary_raw=salary, experience=fields["experience"], education=fields["education"],
            recruiting_entity=fields["company"],
        )
        # These navigation values remain in memory only; never put them in evidence or prompts.
        navigation = url
        if kind == "boss":
            params = {key: raw[key] for key in ("securityId", "lid") if raw.get(key)}
            if params:
                navigation += "?" + urlencode(params)
        jobs.append((doc, navigation))
    return jobs, True, len(raw_items)


def collect_platform(account_id, kind, binding, identity, filters, progress=None, *, descriptor=None, runtime_scope=None):
    from app.adapter_maintenance import fingerprint, use_manifest
    from app.repair_evidence import attach_evidence, capture_repairs
    manifest = descriptor.manifest if descriptor is not None else contract(kind)
    digest = fingerprint(manifest)
    if binding.get("expected_adapter_fingerprint") and binding["expected_adapter_fingerprint"] != digest:
        raise CollectionError("adapter_version_conflict", "恢复任务的适配器版本已变化，未混用新版本继续采集")
    runtime = None
    if manifest.get("program"):
        from app.adapter_code_runtime import ProgramRuntime, freeze_active_adapter
        if runtime_scope is None:
            raise CollectionError("code_runtime_scope", "代码采集必须关联实际公司来源任务")
        descriptor = descriptor or freeze_active_adapter(kind)
        if descriptor.fingerprint != digest:
            raise CollectionError("adapter_version_conflict", "代码版本在建立执行前变化")
        runtime = ProgramRuntime(descriptor, binding_snapshot=binding, **runtime_scope)
    # One immutable release for this collection, including all response callbacks.
    with use_manifest(kind, manifest), capture_repairs(kind, binding, identity) as capture:
        try:
            result = _collect_platform(account_id, kind, binding, identity, filters, progress,
                                       mapping=manifest, repair_state=capture, program_runtime=runtime,
                                       probe_scope=runtime_scope)
        except CollectionError as exc:
            if runtime is not None:
                exc.code_execution = {**runtime.evidence(), "verified": False, "error_code": exc.code}
                exc.adapter_fingerprint = digest
            raise attach_evidence(exc, capture, digest)
    result.adapter_fingerprint = digest
    if runtime is not None:
        result.code_execution = runtime.evidence()
    return result


def _collect_platform(account_id, kind, binding, identity, filters, progress=None, *,
                      mapping=None, repair_state=None, program_runtime=None, probe_scope=None):
    mapping = contract(kind) if mapping is None else mapping
    source_host = urlparse(binding["entry_url"]).hostname or ""
    root = PLATFORM_ROOTS[kind]
    if source_host != root and not source_host.endswith("." + root):
        raise CollectionError("source_host_mismatch", "公司页域名与招聘平台不一致")
    probe = binding.get("browser_session")
    state = {"cookies": [], "origins": []} if probe else load_state(account_id) if account_id else None
    if state is None and kind in PUBLIC_PLATFORMS and program_runtime is None:
        from app.platform_documents import collect_public_platform
        try:
            return collect_public_platform(kind, binding, identity, filters, progress)
        except CollectionError as exc:
            if exc.code != "adaptation_required":
                raise
        state = {"cookies": [], "origins": []}
    if state is None and kind in PUBLIC_PLATFORMS and program_runtime is not None:
        state = {"cookies": [], "origins": []}
    if state is None:
        raise CollectionError("waiting_login", "该账号尚未保存登录会话")
    entry_url = binding["entry_url"]
    if kind in PUBLIC_PLATFORMS:
        from app.platform_documents import listing_entry
        entry_url = listing_entry(kind, binding)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(get_settings().browser_cache_dir)
    result = CollectionResult()
    captured = {}
    capture = {"recognized": False, "raw_count": 0, "error": None, "program_error": None}
    raw_responses = []
    observed_responses = set()
    last_program_context = None
    page_limit = min(filters.get("max_pages", 3), get_settings().task_max_pages, 10)
    detail_limit = min(filters.get("max_details", 20), get_settings().task_max_details)
    start = time.monotonic()

    def invoke_program(program_context):
        nonlocal last_program_context
        from app.adapter_program import capture_program_failure
        last_program_context = program_context
        try:
            return program_runtime.invoke(program_context)
        except CollectionError as exc:
            exc.code_failure_evidence = capture_program_failure(program_context)
            raise

    from app.probe_browser import ProbePlaywright
    provider = ProbePlaywright(account_id, probe, probe_scope, mapping["response_path_pattern"]) if probe else sync_playwright()
    with provider as playwright:
        browser = playwright.chromium.launch(channel="chromium", headless=True)
        context = browser.new_context(storage_state=state, locale="zh-CN",
                                      viewport=VIEWPORT, timezone_id="Asia/Shanghai",
                                      service_workers="block", accept_downloads=False)
        page = context.new_page()
        page.set_default_timeout(12000)

        def guard(route):
            try:
                validate_fetch_url(route.request.url)
                if route.request.is_navigation_request() and route.request.frame == route.request.frame.page.main_frame:
                    if program_runtime is not None:
                        program_runtime.check()
                    host = urlparse(route.request.url).hostname or ""
                    if host != root and not host.endswith("." + root):
                        route.abort()
                        return
                route.continue_()
            except CollectionError as exc:
                capture["program_error"] = exc
                route.abort()

        context.route("**/*", guard)

        def response_received(response):
            host = urlparse(response.url).hostname or ""
            if host != root and not host.endswith("." + root):
                return
            if not re.search(mapping["response_path_pattern"], urlparse(response.url).path, re.I):
                return
            if response.status in {401, 403, 429}:
                capture["error"] = "waiting_login" if response.status == 401 else "waiting_verification"
                return
            if "json" not in response.headers.get("content-type", ""):
                return
            try:
                body = response.body()
                if len(body) > 3_000_000:
                    return
                payload = json.loads(body)
                message = str(payload.get("message") or payload.get("msg")
                              or path_value(payload, ["resultbody", "error_msg"]) or "")
                if re.search(r"未登录|请.*登录|登录.*失效", message):
                    capture["error"] = "waiting_login"
                    return
                if re.search(r"安全验证|环境.*异常|访问.*频繁|风控|验证码", message) or payload.get("code") in {31, 37}:
                    capture["error"] = "waiting_verification"
                    return
                # Playwright dispatches this callback in a fresh EventGreenlet. Use
                # this run's captured values, never ContextVar lookups in callbacks.
                if program_runtime is not None:
                    from app.adapter_code_package import content_sha256
                    marker = content_sha256(payload)
                    if marker not in observed_responses:
                        if len(raw_responses) >= 4:
                            capture["program_error"] = CollectionError("code_response_budget", "单页响应样本超过代码解析预算")
                        else:
                            raw_responses.append((response.url, payload, now()))
                            observed_responses.add(marker)
                    return
                items, recognized, count = extract_jobs(payload, kind, binding, identity, mapping=mapping)
                if not recognized:
                    from app.repair_evidence import capture_response
                    capture_response(payload, mapping, state=repair_state)
                capture["recognized"] |= recognized
                capture["raw_count"] += count
                for doc, navigation in items:
                    captured[doc.source_job_id] = (doc, navigation)
            except Exception:
                # Malformed/foreign responses cannot become job data.
                return

        page.on("response", response_received)

        def navigate(target, url, timeout):
            if program_runtime is not None:
                program_runtime.check()
            try:
                response = target.goto(url, wait_until="domcontentloaded", timeout=timeout)
            except Exception:
                if capture["program_error"]:
                    raise capture["program_error"]
                raise
            if capture["program_error"]:
                raise capture["program_error"]
            check_navigation(response)

        try:
            navigate(page, entry_url, 25000)
            for index in range(page_limit):
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
                except Exception:
                    pass
                result.pages = index + 1
                if capture["error"]:
                    raise CollectionError(capture["error"], "平台要求重新登录或人工验证，请打开账号登录会话处理")
                require_readable_page(page.locator("body").inner_text(timeout=5000))
                if capture["program_error"]:
                    raise capture["program_error"]
                if program_runtime is not None:
                    from app.adapter_program import prepare_program_input
                    from app.platform_documents import check_platform_page, program_listing_payload
                    html = page.content()
                    check_platform_page(html, kind)
                    queued = list(raw_responses)
                    raw_responses.clear()
                    embedded = program_listing_payload(html, kind)
                    if embedded or not queued:
                        queued.append((page.url, embedded, now()))
                    for _, payload, captured_at in queued:
                        program_context = prepare_program_input(kind, binding, identity, operation="parse_list",
                            page_url=page.url, payload=payload, html=html)
                        parsed = invoke_program(program_context)
                        for doc in parsed.records:
                            doc.observed_at = captured_at
                        capture["recognized"] |= parsed.recognized
                        capture["raw_count"] += parsed.raw_count
                        captured.update({doc.source_job_id: (doc, navigation) for doc, navigation in parsed.jobs})
                elif kind in PUBLIC_PLATFORMS:
                    from app.platform_documents import capture_document_failure, check_platform_page, document_jobs
                    html = page.content()
                    check_platform_page(html, kind)
                    items, recognized, count = document_jobs(html, kind, binding, identity)
                    capture["recognized"] |= recognized
                    capture["raw_count"] += count
                    captured.update({doc.source_job_id: (doc, navigation) for doc, navigation in items})
                    if not recognized:
                        capture["code_failure_evidence"] = capture_document_failure(html, kind, binding, identity,
                            page_url=page.url, operation="parse_list")
                if progress:
                    progress(f"{kind} 已观察 {len(captured)} 条匹配公司的岗位")
                if len(captured) >= detail_limit or index + 1 == page_limit:
                    break
                if program_runtime is not None:
                    program_context = prepare_program_input(kind, binding, identity, operation="navigation",
                        page_url=page.url, html=page.content())
                    actions = invoke_program(program_context)
                    if not actions:
                        break
                    followed = False
                    for action in actions:
                        program_runtime.check()
                        if action.type == "follow":
                            if followed or action.url == page.url:
                                continue
                            navigate(page, action.url, 25000)
                            followed = True
                            break  # One navigation consumes the next page-budget slot.
                        if action.type == "scroll":
                            page.mouse.wheel(0, action.pixels)
                        elif action.type == "wait":
                            page.wait_for_timeout(action.milliseconds)
                else:
                    page.mouse.wheel(0, 1500)
                    page.wait_for_timeout(700)
                if time.monotonic() - start > get_settings().task_timeout_seconds:
                    break
            if not captured:
                if capture["raw_count"]:
                    raise CollectionError("identity_review", "岗位中的公司与关注主体不一致，请检查公司页、别名或平台公司标识")
                error = CollectionError("source_contract_changed", "未识别到可验证的岗位数据，需要适配；没有将其当作零招聘")
                if last_program_context is not None:
                    from app.adapter_program import capture_program_failure
                    error.code_failure_evidence = capture_program_failure(last_program_context)
                elif capture.get("code_failure_evidence"):
                    error.code_failure_evidence = capture["code_failure_evidence"]
                raise error
            detail_page = context.new_page()
            for doc, navigation in list(captured.values())[:detail_limit]:
                navigate(detail_page, navigation, 20000)
                try:
                    detail_page.wait_for_load_state("networkidle", timeout=1500)
                except Exception:
                    pass
                whole_text = detail_page.locator("body").inner_text(timeout=5000)
                require_readable_page(whole_text)
                from app.platform_documents import check_job_detail_url
                check_job_detail_url(kind, doc, detail_page.url)
                if program_runtime is not None:
                    from app.adapter_program import prepare_program_input
                    from app.platform_documents import program_detail_payload
                    html = detail_page.content()
                    captured_at = now()
                    payload = program_detail_payload(html, kind, doc, binding, identity)
                    program_context = prepare_program_input(kind, binding, identity, operation="parse_detail",
                        page_url=detail_page.url, html=html, payload=payload, job=doc)
                    doc, _ = invoke_program(program_context)
                    doc.observed_at = captured_at
                elif kind in PUBLIC_PLATFORMS:
                    from app.platform_documents import capture_document_failure, document_detail
                    try:
                        doc = document_detail(detail_page.content(), kind, doc, binding, identity)
                    except CollectionError as exc:
                        exc.code_failure_evidence = capture_document_failure(detail_page.content(), kind, binding, identity,
                            page_url=detail_page.url, operation="parse_detail", job=doc)
                        raise
                for selector in [] if program_runtime is not None else mapping["detail_selectors"]:
                    if doc.description:
                        break
                    locators = detail_page.locator(selector)
                    if locators.count():
                        doc.description = locators.first.inner_text(timeout=3000).strip()
                        if doc.description:
                            break
                if not doc.description:
                    if kind not in PUBLIC_PLATFORMS:
                        from app.repair_evidence import capture_detail
                        capture_detail(detail_page.content(), doc)
                    result.errors.append("一个岗位的职责区域未识别")
                    continue
                doc.raw_ref = doc.url
                result.records.append(doc)
                # Only normalized public job fields are retained, not the authenticated page or response.
                result.documents.append((doc.url, doc.model_dump_json(exclude={"raw_ref"}), "application/json"))
                if progress:
                    progress(f"{kind} 已读取 {len(result.records)} 条岗位详情")
                if time.monotonic() - start > get_settings().task_timeout_seconds:
                    break
            if not result.records:
                raise CollectionError("source_contract_changed", "岗位详情未能验证，未保存缺失正文的岗位")
            if account_id and not probe:
                save_state(account_id, context.storage_state(indexed_db=True))
            result.coverage = "partial"
            result.stop_reason = "按平台账号可访问范围有限采集；尚未证明全量覆盖"
            return result
        except CollectionError as exc:
            if probe and result.records and exc.code in {
                "waiting_device", "chrome_closed", "device_paused", "probe_response_timeout",
                "execution_unknown", "browser_operation_failed",
            }:
                result.coverage = "partial"
                result.stop_reason = "设备浏览器读取中断，保留已核验岗位，未推断下架"
                result.errors.append(exc.message)
                return result
            raise
        finally:
            context.close()
            browser.close()
