"""A bounded search transport with one explicitly deployed internal endpoint.

The private-origin exception belongs only to this search API. Candidate/website
URLs continue through the ordinary public-network fetcher.
"""
from dataclasses import dataclass
import hashlib
import json
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import select

from app.collection import CollectionError
from app.config import get_settings
from app.recruitment_management_models import DiscoverySearchConfig
from app.repair_evidence import redact_text
from app.serialization import row

MAX_RESPONSE_BYTES = 2_000_000
MAX_RESULT_ROWS = 100


def deployment_endpoint():
    value = get_settings().discovery_internal_search_url.strip()
    if not value:
        return None
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment
                 and parsed.path.endswith("/search") and not any(ord(c) < 33 for c in value)
                 and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise CollectionError("search_configuration_invalid", "部署的搜索地址格式不正确，请检查服务配置")
    return value


def validate_search_endpoint(value):
    from app.recruitment_discovery import public_url_syntax
    value = value.strip()
    parsed = urlparse(value)
    if (parsed.port == 0 or parsed.username is not None or parsed.password is not None
            or any(ord(character) < 33 for character in value)):
        raise ValueError("搜索接口不能包含凭据、空白控制字符或无效端口")
    try:
        trusted = deployment_endpoint()
    except CollectionError as exc:
        raise ValueError(exc.message) from exc
    if trusted and value == trusted:
        return value
    return public_url_syntax(value)


def effective_search_configuration(db):
    stored = db.scalar(select(DiscoverySearchConfig).where(DiscoverySearchConfig.name == "default"))
    if stored:
        return {**row(stored), "origin": "saved"}
    endpoint = deployment_endpoint()
    return {"endpoint_url": endpoint, "enabled": True, "max_results": 5, "origin": "deployment"} if endpoint else None


@dataclass
class SearchResponse:
    results: list
    evidence: dict


def _search_error(code, message, evidence=None):
    error = CollectionError(code, message)
    error.search_evidence = evidence or {}
    return error


def _internal_body(endpoint, query, *, transport=None):
    settings = get_settings()
    if settings.app_env == "test" and transport is None:
        raise CollectionError("search_test_network_forbidden", "测试环境必须显式提供搜索传输替身")
    timeout = max(1, min(settings.discovery_search_timeout_seconds, 30))
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport,
                          headers={"Accept": "application/json", "User-Agent": "HR-Intelligence/0.1"}) as client:
            with client.stream("GET", endpoint, params={"q": query, "format": "json", "language": "zh-CN"}) as response:
                if time.monotonic() - started >= timeout:
                    raise CollectionError("search_timeout", "搜索等待预算已用完，候选未更新")
                if 300 <= response.status_code < 400:
                    raise CollectionError("search_redirect", "内部搜索服务发生跳转，请检查固定接口配置")
                if response.status_code in {401, 403, 429}:
                    raise CollectionError("search_access_restricted", "搜索服务限制访问，请检查服务状态后重试")
                if response.status_code >= 400:
                    raise CollectionError("search_http_error", f"搜索服务返回 HTTP {response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if time.monotonic() - started >= timeout:
                        raise CollectionError("search_timeout", "搜索读取预算已用完，候选未更新")
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise CollectionError("search_response_too_large", "搜索响应超过大小预算")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8")
    except httpx.TimeoutException as exc:
        raise CollectionError("search_timeout", "搜索服务超时，候选未更新，请稍后重试") from exc
    except httpx.HTTPError as exc:
        raise CollectionError("search_unavailable", "搜索服务连接未完成，请检查部署状态") from exc
    except UnicodeDecodeError as exc:
        raise CollectionError("search_contract_changed", "搜索服务未返回有效的UTF-8 JSON") from exc


def read_search_response(endpoint, query, public_fetcher, *, transport=None):
    try:
        endpoint = validate_search_endpoint(endpoint)
    except ValueError as exc:
        raise CollectionError("search_configuration_invalid", "搜索服务地址不符合配置要求") from exc
    if endpoint == deployment_endpoint():
        body = _internal_body(endpoint, query, transport=transport)
    else:
        parsed = urlparse(endpoint)
        params = {**dict(parse_qsl(parsed.query)), "q": query, "format": "json", "language": "zh-CN"}
        try:
            final, body = public_fetcher.fetch(urlunparse(parsed._replace(query=urlencode(params))), allow_redirects=False)
        except CollectionError as exc:
            if exc.code == "redirect_disallowed":
                raise CollectionError("search_redirect", "搜索固定接口发生跳转，未读取其他地址") from exc
            raise
        final_parsed = urlparse(final)
        if (final_parsed.scheme, final_parsed.netloc, final_parsed.path) != (parsed.scheme, parsed.netloc, parsed.path):
            raise CollectionError("search_redirect", "搜索响应没有返回配置中的固定接口")
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise CollectionError("search_response_too_large", "搜索响应超过大小预算")
    try:
        payload = json.loads(body)
        if isinstance(payload, dict) and payload.get("error"):
            raise _search_error("search_upstream_failed", "搜索服务报告错误，请检查服务状态；原候选保留",
                {"provider": "searxng", "query": query, "source_url": endpoint, "request_status": "failed",
                 "provider_error": redact_text(str(payload["error"]))[:200]})
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError()
        if "query" in payload and payload["query"] != query:
            raise CollectionError("search_query_mismatch", "搜索服务返回了其他查询的结果，候选未更新")
        failures = payload.get("unresponsive_engines", [])
        if not isinstance(failures, list):
            raise ValueError()
        errors = []
        for item in failures[:10]:
            if not isinstance(item, (list, tuple)) or len(item) < 2 or not all(isinstance(value, str) for value in item[:2]):
                raise ValueError()
            errors.append({"engine": redact_text(item[0])[:80], "reason": redact_text(item[1])[:200]})
    except (ValueError, TypeError) as exc:
        raise CollectionError("search_contract_changed", "搜索服务未返回 SearXNG JSON results 契约") from exc
    evidence = {"query": query, "provider": "searxng", "source_url": endpoint,
                "response_text_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "returned_results": len(payload["results"]), "inspected_result_limit": MAX_RESULT_ROWS,
                "unresponsive_engines": errors,
                "request_status": "partial" if errors else "succeeded",
                "scope": "本次搜索返回的有限候选，尚未核实公司或岗位覆盖"}
    if errors and not payload["results"]:
        raise _search_error("search_upstream_failed", "搜索引擎未完成查询，请稍后重试；原候选保留",
                            {**evidence, "request_status": "failed"})
    return SearchResponse(payload["results"][:MAX_RESULT_ROWS], evidence)
