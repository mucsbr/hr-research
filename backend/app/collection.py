import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings


class CollectionError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class JobDocument(BaseModel):
    source_job_id: str
    title: str = Field(min_length=1, max_length=500)
    url: str
    locations: list[str] = Field(default_factory=list)
    department: str | None = None
    function: str | None = None
    experience: str | None = None
    education: str | None = None
    salary_raw: str | None = None
    currency: str | None = None
    description: str = ""
    requirements: str = ""
    recruiting_entity: str | None = None
    source_published_at: str | None = None
    source_updated_at: str | None = None
    raw_ref: str | None = None
    observed_at: datetime | None = None

    @field_validator("observed_at")
    @classmethod
    def observed_timestamp(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("观察时间必须带时区")
        return value

    def content_hash(self):
        data = self.model_dump(exclude={
            "raw_ref", "source_published_at", "source_updated_at", "source_job_id", "url", "observed_at"
        })
        for key, value in data.items():
            if isinstance(value, str):
                data[key] = re.sub(r"\s+", " ", value).strip()
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class CollectionResult:
    records: list[JobDocument] = field(default_factory=list)
    documents: list[tuple[str, str, str]] = field(default_factory=list)
    coverage: Literal["complete", "partial", "unknown"] = "unknown"
    pages: int = 0
    stop_reason: str = ""
    errors: list[str] = field(default_factory=list)
    adapter_fingerprint: str = ""
    checkpoint_info: dict = field(default_factory=dict)
    code_execution: dict = field(default_factory=dict)


_dns_cache: dict[str, tuple[float, str]] = {}


def validate_fetch_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise CollectionError("invalid_url", "来源网址必须是 HTTP/HTTPS")
    if parsed.username or parsed.password:
        raise CollectionError("invalid_url", "来源网址不能携带凭据")
    settings = get_settings()
    fixture = settings.allow_local_fixture_fetch and parsed.hostname in {"127.0.0.1", "localhost"}
    if fixture:
        return "127.0.0.1"
    cached = _dns_cache.get(parsed.hostname)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise CollectionError("dns_error", "无法解析来源域名") from exc
    ips = sorted({a[4][0] for a in addresses})
    if ips and settings.public_dns_fallback and all(
        ipaddress.ip_address(address) in ipaddress.ip_network("198.18.0.0/15")
        for address in ips
    ):
        # TUN fake-IP networks: resolve through a fixed public resolver, then connect
        # to the verified public IP. Never permit arbitrary private destinations.
        try:
            response = httpx.get(
                "https://dns.alidns.com/resolve" if settings.public_dns_resolver == "alidns" else "https://dns.google/resolve",
                params={"name": parsed.hostname, "type": "A"},
                timeout=10, trust_env=False,
            )
            response.raise_for_status()
            answer = response.json()
            ips = [item["data"] for item in answer.get("Answer", []) if item.get("type") == 1]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise CollectionError("dns_error", "公网 DNS 验证失败") from exc
    if not ips or any(not ipaddress.ip_address(address).is_global for address in ips):
        raise CollectionError("restricted_address", "数据采集不允许访问私有或保留网络地址")
    address = next((ip for ip in ips if ":" not in ip), ips[0])
    if len(_dns_cache) > 256:
        _dns_cache.clear()
    _dns_cache[parsed.hostname] = (time.monotonic() + 60, address)
    return address


class PublicFetcher:
    """Bounded public document reads; validate each redirect and never forward API credentials."""

    def __init__(self, transport=None, check=None):
        self.clients: dict[str, httpx.Client] = {}
        self.transport = transport
        self.check = check

    def close(self):
        for client in self.clients.values():
            client.close()

    def fetch(self, url: str, *, allow_redirects=True) -> tuple[str, str]:
        for _ in range(6):
            if self.check:
                self.check()
            address = validate_fetch_url(url)
            parsed = urlparse(url)
            host = parsed.hostname
            if host not in self.clients:
                # Pools are separated by original hostname even if origins share one IP.
                self.clients[host] = httpx.Client(
                    timeout=25, follow_redirects=False, trust_env=False,
                    transport=self.transport,
                    headers={"User-Agent": "HR-Intelligence/0.1 (public recruitment research)",
                             "Accept": "text/html,application/json"},
                )
            pinned = httpx.URL(url).copy_with(host=address)
            with self.clients[host].stream(
                "GET", pinned, headers={"Host": parsed.netloc},
                extensions={"sni_hostname": host},
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not allow_redirects:
                        raise CollectionError("redirect_disallowed", "固定接口发生跳转，未读取其他地址")
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                if response.status_code in {401, 403, 429}:
                    raise CollectionError("access_restricted", "来源要求登录、验证或限制访问")
                if response.status_code >= 400:
                    raise CollectionError("http_error", f"来源返回 HTTP {response.status_code}")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    if self.check:
                        self.check()
                    size += len(chunk)
                    if size > 3_000_000:
                        raise CollectionError("document_too_large", "单页内容超过采集预算")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                return url, raw.decode(response.encoding or "utf-8", errors="replace")
        raise CollectionError("redirect_limit", "来源重定向次数过多")


def text_value(value):
    if isinstance(value, str):
        return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    if isinstance(value, dict):
        return str(value.get("name", value.get("value", "")))
    if isinstance(value, list):
        return "、".join(filter(None, (text_value(v) for v in value)))
    return "" if value is None else str(value)


def require_readable_page(html: str):
    """Keep blocked responses out of successful snapshots and inferred delistings."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select("script,style"):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    if re.search(r"完成(?:安全)?验证|安全验证|环境异常|访问(?:过于)?频繁", text):
        raise CollectionError("waiting_verification", "页面需要人工验证，未将受限页面作为岗位数据")
    if re.search(r"登录后.{0,12}(查看|访问)|请先登录|登录查看完整|登录.{0,8}失效", text):
        raise CollectionError("waiting_login", "页面需要有效登录，未将登录页作为岗位数据")


def confirmed_end_marker(marker: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:没有更多(?:职位|岗位|内容)?|暂无更多(?:职位|岗位|内容)?|"
        r"(?:已)?全部加载(?:完成|完毕)|no more (?:jobs|positions)|end of (?:jobs|positions))\s*[。.!]?\s*",
        marker, re.I,
    ))


def jsonld_jobs(html: str, page_url: str) -> list[JobDocument]:
    soup = BeautifulSoup(html, "html.parser")
    items = []

    def walk(value):
        if isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, dict):
            types = value.get("@type", "")
            if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
                items.append(value)
            for key in ("@graph", "itemListElement", "item"):
                if key in value:
                    walk(value[key])

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    result = []
    for item in items:
        title = text_value(item.get("title"))
        if not title:
            continue
        location = item.get("jobLocation", [])
        if not isinstance(location, list):
            location = [location]
        locations = []
        for loc in location:
            address = loc.get("address", {}) if isinstance(loc, dict) else {}
            if isinstance(address, dict):
                place = " ".join(str(address[k]) for k in
                                 ("addressCountry", "addressRegion", "addressLocality")
                                 if address.get(k) and isinstance(address[k], str))
                if place:
                    locations.append(place)
        job_url = urljoin(page_url, item.get("url") or page_url)
        identifier = text_value(item.get("identifier")) or job_url
        salary = item.get("baseSalary")
        result.append(JobDocument(
            source_job_id=identifier[:500], title=title, url=job_url, locations=locations,
            description=text_value(item.get("description")),
            requirements=text_value(item.get("qualifications")),
            experience=text_value(item.get("experienceRequirements")) or None,
            education=text_value(item.get("educationRequirements")) or None,
            recruiting_entity=text_value(item.get("hiringOrganization")) or None,
            salary_raw=json.dumps(salary, ensure_ascii=False) if salary else None,
            currency=salary.get("currency") if isinstance(salary, dict) else None,
            source_published_at=item.get("datePosted"), raw_ref=page_url,
        ))
    return result


def collect_generic(binding: dict, filters: dict, progress=None, *, checkpoint=None) -> CollectionResult:
    if checkpoint is not None:
        from app.checkpoint_collectors import collect_generic as resumable
        return resumable(binding, filters, progress, checkpoint)
    result = CollectionResult()
    config = binding.get("config", {})
    limit = min(filters.get("max_pages", 10), get_settings().task_max_pages)
    detail_limit = min(filters.get("max_details", 50), get_settings().task_max_details)
    fetcher = PublicFetcher()
    pending, visited, seen = [binding["entry_url"]], set(), set()
    truncated = False
    confirmed_end = False
    try:
        while pending and result.pages < limit:
            url = pending.pop(0)
            if url in visited:
                continue
            visited.add(url)
            final_url, html = fetcher.fetch(url)
            require_readable_page(html)
            result.pages += 1
            result.documents.append((final_url, html, "text/html"))
            soup = BeautifulSoup(html, "html.parser")
            page_jobs = jsonld_jobs(html, final_url)
            for doc in page_jobs:
                if doc.source_job_id not in seen:
                    if len(result.records) >= detail_limit:
                        truncated = True
                        break
                    seen.add(doc.source_job_id)
                    result.records.append(doc)
            for selector in (config.get("job_link_selector"), config.get("next_selector")):
                for node in soup.select(selector) if selector else []:
                    href = node.get("href")
                    if href:
                        target = urljoin(final_url, href).split("#", 1)[0]
                        parsed = urlparse(target)
                        if parsed.scheme not in {"http", "https"} or parsed.hostname != urlparse(final_url).hostname:
                            result.errors.append("链接超出当前官网域名，未自动扩大采集范围")
                            continue
                        if target not in visited and target not in pending:
                            pending.append(target)
            end_node = soup.select_one(config["complete_selector"]) if config.get("complete_selector") else None
            confirmed_end = bool(end_node and confirmed_end_marker(end_node.get_text(" ", strip=True)))
            if progress:
                progress(f"已读取 {result.pages} 页，提取 {len(result.records)} 个岗位")
            if truncated or len(result.records) >= detail_limit and pending:
                result.stop_reason = "岗位数量预算"
                result.coverage = "partial"
                break
        if truncated or pending:
            result.coverage = "partial"
            result.stop_reason = result.stop_reason or "页面预算或尚有未处理页面"
        elif confirmed_end and not result.errors:
            result.coverage = "complete"
            result.stop_reason = "已验证配置范围的结束标记"
        else:
            result.coverage = "unknown"
            result.stop_reason = "已提取公开结构化岗位，尚未证明覆盖全部列表"
        if not result.records:
            if result.coverage == "complete":
                return result
            raise CollectionError("adaptation_required", "此入口尚未识别到结构化岗位，需要浏览器或官网适配")
        return result
    finally:
        fetcher.close()


def matches_filters(doc: JobDocument, filters: dict):
    combined = " ".join([doc.title, doc.description, doc.requirements,
                         " ".join(doc.locations)]).casefold()
    cities = filters.get("cities", [])
    if cities and not any(city.casefold() in " ".join(doc.locations).casefold() for city in cities):
        return False
    keywords = filters.get("keywords", [])
    if keywords and not any(keyword.casefold() in combined for keyword in keywords):
        return False
    return not any(exclusion.casefold() in combined for exclusion in filters.get("exclusions", []))


def collect_tencent(binding: dict, filters: dict, progress=None, *, checkpoint=None) -> CollectionResult:
    if checkpoint is not None:
        from app.checkpoint_collectors import collect_tencent as resumable
        return resumable(binding, filters, progress, checkpoint)
    if urlparse(binding["entry_url"]).hostname != "careers.tencent.com":
        raise CollectionError("adapter_host_mismatch", "腾讯适配器只能绑定腾讯官方招聘域名")
    result = CollectionResult()
    fetcher = PublicFetcher()
    pages = min(filters.get("max_pages", 10), get_settings().task_max_pages)
    details = min(filters.get("max_details", 50), get_settings().task_max_details)
    total, visited = None, set()
    try:
        for page in range(1, pages + 1):
            query = urlencode({"pageIndex": page, "pageSize": min(10, details),
                               "language": "zh-cn", "area": "cn"})
            url, body = fetcher.fetch("https://careers.tencent.com/tencentcareer/api/post/Query?" + query)
            payload = json.loads(body)
            data = payload.get("Data")
            if payload.get("Code") != 200 or not isinstance(data, dict) or not isinstance(data.get("Posts"), list):
                raise CollectionError("source_contract_changed", "腾讯岗位列表数据结构发生变化")
            result.documents.append((url, body, "application/json"))
            result.pages += 1
            total = data.get("Count")
            posts = data["Posts"]
            for post in posts:
                key = str(post.get("PostId") or "")
                if not key or key in visited:
                    continue
                if len(visited) >= details:
                    break
                visited.add(key)
                detail_url = "https://careers.tencent.com/tencentcareer/api/post/ByPostId?" + urlencode(
                    {"postId": key, "language": "zh-cn"}
                )
                raw_url, raw = fetcher.fetch(detail_url)
                detail_response = json.loads(raw)
                detail = detail_response.get("Data")
                if detail_response.get("Code") != 200 or not isinstance(detail, dict):
                    result.errors.append("一个岗位详情未成功读取")
                    continue
                result.documents.append((raw_url, raw, "application/json"))
                result.records.append(JobDocument(
                    source_job_id=key, title=detail["RecruitPostName"],
                    url="https://careers.tencent.com/jobdesc.html?postId=" + key,
                    locations=[detail.get("LocationName", "")],
                    department=detail.get("BGName"),
                    function=detail.get("CategoryName"),
                    experience=detail.get("RequireWorkYearsName"),
                    recruiting_entity=detail.get("ComName"),
                    description=text_value(detail.get("Responsibility")),
                    requirements=text_value(detail.get("Requirement")),
                    source_updated_at=detail.get("LastUpdateTime"),
                    raw_ref=raw_url,
                ))
                if progress:
                    progress(f"官网岗位详情 {len(result.records)} / 本次上限 {details}")
            if isinstance(total, int) and len(visited) >= total:
                result.coverage = "complete" if not result.errors else "partial"
                result.stop_reason = "已遍历来源列表" if not result.errors else "部分详情失败"
                return result
            if not posts:
                # An empty response alone is not proof that all previous positions vanished.
                result.coverage = "complete" if total == 0 and page == 1 else "unknown"
                result.stop_reason = "来源返回空列表"
                return result
            if len(visited) >= details:
                break
        result.coverage = "partial"
        result.stop_reason = f"达到本次预算；来源显示 {total} 个岗位，不代表已全部采集"
        return result
    finally:
        fetcher.close()
