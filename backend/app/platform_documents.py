"""Bounded public-document paths for Chinese recruitment platforms.

Only JSON embedded in an explicit state assignment is decoded. Site scripts,
anti-bot challenges and account secrets are never executed by this parser.
"""
import json
import re
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.collection import (
    CollectionError, CollectionResult, PublicFetcher, jsonld_jobs,
    require_readable_page, text_value,
)
from app.config import get_settings


def initial_state(html):
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select("script:not([src])"):
        text = script.string or script.get_text()
        match = re.match(r"\s*(?:window\.)?__INITIAL_STATE__\s*=\s*", text)
        if not match:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end():])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def check_platform_page(html, kind):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one('meta[name^="aliyun_waf_"]'):
        raise CollectionError("waiting_verification", "平台返回访问校验页，请在账号会话中人工处理")
    require_readable_page(html)
    if kind == "zhaopin":
        detail = initial_state(html).get("jobDetail", {})
        clamp = detail.get("guestJobDescClamp", {}) if isinstance(detail, dict) else {}
        if isinstance(clamp, dict):
            visible, total = clamp.get("visibleLines"), clamp.get("totalLines")
            if isinstance(visible, int) and isinstance(total, int) and total > visible:
                raise CollectionError("waiting_login", "智联岗位正文标记为访客截断，请登录后采集")


def document_jobs(html, kind, binding, identity):
    from app.platform_collectors import extract_jobs

    check_platform_page(html, kind)
    if kind == "zhaopin":
        return extract_jobs(initial_state(html), kind, binding, identity)
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    # The current public frontend renders card tracking fields plus its job link.
    # Requiring both avoids treating generic SEO/category links as jobs.
    cards = soup.select('.card[sensorsdata], .joblist-item[sensorsdata]')
    if not cards:
        return [], False, 0
    for card in cards:
        try:
            tracking = json.loads(card["sensorsdata"])
        except (ValueError, TypeError):
            continue
        if not isinstance(tracking, dict):
            continue
        job_id = str(tracking.get("jobId") or "")
        anchor = card.select_one('a[href*="jobs.51job.com/"]')
        path = urlparse(anchor["href"]).path if anchor else ""
        match = re.fullmatch(r"/([a-zA-Z0-9-]+)/(\d+)\.html", path)
        if not match or match.group(2) != job_id:
            continue
        company = card.select_one(".company-name, .companyName, .company")
        rows.append({
            "jobId": job_id, "jobName": tracking.get("jobTitle"),
            "coId": tracking.get("companyId"),
            "companyName": company.get_text(" ", strip=True) if company else None,
            "provideSalaryString": tracking.get("jobSalary"),
            "jobAreaString": tracking.get("jobArea"),
            "workYearString": tracking.get("jobYear"),
            "degreeString": tracking.get("jobDegree"), "hrefAreaPinYin": match.group(1),
        })
    return extract_jobs({"resultbody": {"items": rows}}, kind, binding, identity)


def document_detail(html, kind, original, binding, identity):
    from app.platform_collectors import expected_company_id, same_company

    check_platform_page(html, kind)
    doc = original.model_copy(deep=True)
    if kind == "zhaopin":
        detail = initial_state(html).get("jobDetail", {})
        if not isinstance(detail, dict):
            raise CollectionError("source_contract_changed", "智联岗位详情结构发生变化")
        position = detail.get("detailedPosition", {})
        company = detail.get("detailedCompany", {})
        if not isinstance(position, dict) or not isinstance(company, dict):
            raise CollectionError("source_contract_changed", "智联岗位详情结构发生变化")
        if str(position.get("positionNumber") or "") != original.source_job_id:
            raise CollectionError("source_contract_changed", "智联详情岗位标识与列表不一致")
        if not same_company(company.get("companyName"), company.get("companyNumber"),
                            kind, binding, identity):
            raise CollectionError("identity_review", "智联详情招聘公司与关注主体不一致")
        doc.title = str(position.get("positionName") or doc.title)[:500]
        doc.description = text_value(position.get("jobDesc"))
        doc.recruiting_entity = company.get("companyName")
        doc.locations = [position["positionWorkCity"]] if position.get("positionWorkCity") else doc.locations
        salary = position.get("salary")
        if isinstance(salary, str) and salary and not re.search(r"[\ue000-\uf8ff*]", salary):
            doc.salary_raw = salary
        doc.source_published_at = position.get("positionPublishTime") or None
    else:
        soup = BeautifulSoup(html, "html.parser")
        company = soup.select_one('.com_name a[href], .company a[href], a.catn[href]')
        company_id = expected_company_id(kind, {"entry_url": company["href"]}) if company else None
        structured = [item for item in jsonld_jobs(html, doc.url)
                      if item.source_job_id == doc.source_job_id or item.url.split("?", 1)[0] == doc.url]
        company_name = company.get_text(" ", strip=True) if company else None
        if not company_name and structured:
            company_name = structured[0].recruiting_entity
        if not same_company(company_name, company_id, kind, binding, identity):
            raise CollectionError("identity_review", "前程无忧详情招聘公司未能与关注主体核对")
        doc.recruiting_entity = company_name
        if structured:
            doc.description = structured[0].description
        else:
            description = soup.select_one(".job_msg, .job-detail .job-description")
            doc.description = description.get_text(" ", strip=True) if description else ""
    if not doc.description.strip():
        raise CollectionError("source_contract_changed", "岗位详情未识别职责正文，未生成占位数据")
    return doc


def program_listing_payload(html, kind):
    """Decode original public state, without replacing source values with inferred fields."""
    check_platform_page(html, kind)
    if kind == "zhaopin":
        return initial_state(html)
    if kind == "51job":
        rows = []
        soup = BeautifulSoup(html, "html.parser")
        for card in soup.select('.card[sensorsdata], .joblist-item[sensorsdata]'):
            try:
                value = json.loads(card["sensorsdata"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                rows.append(value)
        return {"tracking_rows": rows}
    return None


def check_job_detail_url(kind, original, final_url):
    """A same-platform redirect is not proof that the page is still the listed job."""
    from app.adapter_program import _link_job_id
    from app.platform_collectors import PLATFORM_ROOTS
    parsed = urlparse(final_url)
    host, root = (parsed.hostname or "").casefold(), PLATFORM_ROOTS[kind]
    if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or not (host == root or host.endswith("." + root))
            or _link_job_id(final_url, kind) != original.source_job_id):
        raise CollectionError("identity_review", "详情跳转后的岗位标识与已核验列表不同，未附加其他岗位正文")


def check_program_detail_identity(html, kind, original, binding, identity):
    """Access and identity gates remain first-party even when extraction code changes."""
    from app.platform_collectors import expected_company_id, same_company
    check_platform_page(html, kind)
    if kind == "zhaopin":
        detail = initial_state(html).get("jobDetail", {})
        position = detail.get("detailedPosition", {}) if isinstance(detail, dict) else {}
        company = detail.get("detailedCompany", {}) if isinstance(detail, dict) else {}
        if not isinstance(position, dict) or str(position.get("positionNumber") or "") != original.source_job_id:
            raise CollectionError("identity_review", "智联详情岗位标识尚未与已核验列表对应")
        if not isinstance(company, dict) or not same_company(company.get("companyName"), company.get("companyNumber"),
                                                          kind, binding, identity):
            raise CollectionError("identity_review", "智联详情招聘主体未核验，程序不能跳过")
    elif kind == "51job":
        soup = BeautifulSoup(html, "html.parser")
        company = soup.select_one('.com_name a[href], .company a[href], a.catn[href]')
        company_id = expected_company_id(kind, {"entry_url": company["href"]}) if company else None
        company_name = company.get_text(" ", strip=True) if company else None
        structured = [item for item in jsonld_jobs(html, original.url)
                      if item.source_job_id == original.source_job_id or item.url.split("?", 1)[0] == original.url]
        if not company_name and structured:
            company_name = structured[0].recruiting_entity
        if not same_company(company_name, company_id, kind, binding, identity):
            raise CollectionError("identity_review", "前程无忧详情招聘主体未核验，程序不能跳过")


def program_detail_payload(html, kind, original, binding, identity):
    check_program_detail_identity(html, kind, original, binding, identity)
    if kind == "zhaopin":
        return {"verified_details": [initial_state(html)["jobDetail"]["detailedPosition"]]}
    return None


def capture_document_failure(html, kind, binding, identity, *, page_url, operation, job=None):
    """Bounded public evidence; extraction failure never bypasses access/identity checks."""
    from app.adapter_program import capture_program_failure, prepare_program_input
    try:
        payload = (program_detail_payload(html, kind, job, binding, identity) if job else
                   program_listing_payload(html, kind))
        context = prepare_program_input(kind, binding, identity, operation=operation,
                                        page_url=page_url, html=html, payload=payload, job=job)
        return capture_program_failure(context)
    except CollectionError:
        return None


def listing_entry(kind, binding):
    from app.platform_collectors import expected_company_id

    entry = binding["entry_url"]
    if kind == "zhaopin":
        company_id = expected_company_id(kind, binding)
        if company_id and "/companydetail/" in urlparse(entry).path:
            return f"https://www.zhaopin.com/companydetail/jobs-{company_id}/"
    return entry


def collect_public_platform(kind, binding, identity, filters, progress=None):
    from app.platform_collectors import PLATFORM_ROOTS

    def checked_fetch(url):
        final, html = fetcher.fetch(url)
        host, root = urlparse(final).hostname or "", PLATFORM_ROOTS[kind]
        if host != root and not host.endswith("." + root):
            raise CollectionError("source_host_mismatch", "招聘页面跳转到非本平台域名，未采信其数据")
        return final, html

    result = CollectionResult()
    page_limit = min(filters.get("max_pages", 3), get_settings().task_max_pages, 10)
    detail_limit = min(filters.get("max_details", 20), get_settings().task_max_details)
    current = listing_entry(kind, binding)
    seen_pages, candidates = set(), {}
    fetcher = PublicFetcher()
    started = time.monotonic()
    try:
        while current and result.pages < page_limit:
            if current in seen_pages:
                break
            seen_pages.add(current)
            final, html = checked_fetch(current)
            try:
                items, recognized, raw_count = document_jobs(html, kind, binding, identity)
            except CollectionError as exc:
                exc.code_failure_evidence = capture_document_failure(html, kind, binding, identity,
                    page_url=final, operation="parse_list")
                raise
            result.pages += 1
            if raw_count and not items:
                raise CollectionError("identity_review", "公开岗位公司与关注主体不匹配")
            candidates.update({doc.source_job_id: (doc, url) for doc, url in items})
            if not candidates and not recognized:
                raise CollectionError("adaptation_required", "公开静态页未识别岗位，需要正常浏览器加载")
            if len(candidates) >= detail_limit:
                break
            soup = BeautifulSoup(html, "html.parser")
            next_link = soup.select_one(
                '.seopager__btn:not(.seopager__btn--disable):not(.seopager__btn__before)'
            ) if kind == "zhaopin" else None
            next_url = urljoin(final, next_link.get("href", "")) if next_link else None
            current = next_url if next_url and urlparse(next_url).hostname == urlparse(final).hostname else None
            if time.monotonic() - started > get_settings().task_timeout_seconds:
                break
        if not candidates:
            raise CollectionError("source_contract_changed", "未取得可验证的公司岗位，未判定为零招聘")
        for doc, url in list(candidates.values())[:detail_limit]:
            final, html = checked_fetch(url)
            check_platform_page(html, kind)
            check_job_detail_url(kind, doc, final)
            try:
                doc = document_detail(html, kind, doc, binding, identity)
            except CollectionError as exc:
                exc.code_failure_evidence = capture_document_failure(html, kind, binding, identity,
                    page_url=final, operation="parse_detail", job=doc)
                raise
            doc.raw_ref = doc.url
            result.records.append(doc)
            # Keep only normalized public fields; never persist embedded account/HR state.
            result.documents.append((doc.url, doc.model_dump_json(exclude={"raw_ref"}), "application/json"))
            if progress:
                progress(f"{kind} 公开读取 {len(result.records)} 条已核对公司与职责的岗位")
            if time.monotonic() - started > get_settings().task_timeout_seconds:
                break
        result.coverage = "partial"
        result.stop_reason = "公开访问范围内有限采集，未证明全量覆盖"
        return result
    finally:
        fetcher.close()
