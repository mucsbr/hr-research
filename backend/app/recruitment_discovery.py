"""Deterministic, evidence-preserving company entry discovery; no implicit watch targets."""
import hashlib
import ipaddress
import json
import re
import unicodedata
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.collection import CollectionError, PublicFetcher, require_readable_page, validate_fetch_url
from app.models import AuditEvent, Company, SourceBinding, Subject, now
from app.recruitment_management_models import RecruitmentEntryCandidate
from app.recruitment_search import effective_search_configuration, read_search_response
from app.schemas import CompanyInput
from app.serialization import row

CAREER = re.compile(r"招聘|招贤|加入我们|加入团队|人才招募|careers?|recruit|vacanc|join.?us|/jobs?(?:/|\b)", re.I)


def normalize_name(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def public_url_syntax(value):
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网址须为不含凭据的 HTTP/HTTPS URL")
    if parsed.hostname.casefold() in {"localhost", "metadata.google.internal"}:
        raise ValueError("不能使用内部地址")
    try:
        if not ipaddress.ip_address(parsed.hostname).is_global:
            raise ValueError("不能使用私有或保留网络地址")
    except ValueError as exc:
        if "不能使用" in str(exc):
            raise
    if len(value) > 2000:
        raise ValueError("网址过长")
    return urlunparse(parsed._replace(fragment=""))


def same_site(url, website):
    host = (urlparse(url).hostname or "").casefold()
    root = (urlparse(website).hostname or "").casefold().removeprefix("www.")
    return bool(root) and (host == root or host.endswith("." + root))


def entry_source_kind(url):
    from app.platform_collectors import PLATFORM_ROOTS
    host = (urlparse(url).hostname or "").casefold()
    return next((kind for kind, root in PLATFORM_ROOTS.items()
                 if host == root or host.endswith("." + root)), "careers")


def bulk_companies(db, entries, actor_id=None):
    existing = db.scalars(select(Company)).all()
    by_name = {}
    for company in existing:
        by_name.setdefault(normalize_name(company.name), []).append(company)
    results = []
    for index, entry in enumerate(entries):
        data = CompanyInput.model_validate(entry)
        normalized = normalize_name(data.name)
        matches = by_name.get(normalized, [])
        if matches:
            # Do not merge same-name legal entities, restore archived objects or overwrite fields.
            results.append({"index": index, "name": data.name, "status": "existing_review",
                            "company_ids": [str(c.id) for c in matches],
                            "reason": "已有同名记录，未自动合并或修改；可选择现有主体或单独添加"})
            continue
        subject = Subject(name=data.name, aliases=data.aliases)
        db.add(subject)
        db.flush()
        company = Company(subject_id=subject.id, **data.model_dump(exclude={"aliases"}))
        db.add(company)
        db.flush()
        by_name[normalized] = [company]
        db.add(AuditEvent(user_id=actor_id, action="company.bulk_created", target=str(company.id)))
        results.append({"index": index, "name": data.name, "status": "created", "company": row(company)})
    return {"items": results, "created": sum(item["status"] == "created" for item in results),
            "review": sum(item["status"] == "existing_review" for item in results)}


def _save_candidate(db, company, url, title, evidence):
    try:
        url = public_url_syntax(url)
    except ValueError:
        return None
    candidate = db.scalar(select(RecruitmentEntryCandidate).where(
        RecruitmentEntryCandidate.company_id == company.id, RecruitmentEntryCandidate.url == url))
    if candidate:
        # A prior user rejection or binding is not silently reversed by discovery.
        return candidate
    candidate = RecruitmentEntryCandidate(company_id=company.id, kind=entry_source_kind(url),
                                           url=url, title=title[:500], evidence=evidence)
    db.add(candidate)
    db.flush()
    return candidate


def discover_entries(db, company, actor_id=None, max_candidates=10):
    if company.archived:
        raise CollectionError("company_archived", "请先恢复该公司再发现入口")
    config = effective_search_configuration(db)
    fetcher = PublicFetcher()
    candidates = []
    try:
        if company.website:
            final, html = fetcher.fetch(company.website)
            require_readable_page(html)
            if not same_site(final, company.website):
                raise CollectionError("identity_review", "已填官网跳转到其他域名，请先核对并更新公司官网")
            soup = BeautifulSoup(html, "html.parser")
            digest = hashlib.sha256(html.encode()).hexdigest()
            for anchor in soup.select("a[href]"):
                label = anchor.get_text(" ", strip=True)
                url = urljoin(final, anchor["href"])
                if not CAREER.search(label + " " + url) or not same_site(url, company.website):
                    continue
                candidate = _save_candidate(db, company, url, label or "官网招聘入口", {
                    "method": "official_link", "source_url": final, "anchor_text": label[:500],
                    "page_sha256": digest, "observed_at": now().isoformat(),
                    "identity_basis": "用户已填写的公司官网；同域链接，绑定前仍需核对主体",
                })
                if candidate and candidate.id not in {item.id for item in candidates}:
                    candidates.append(candidate)
                if len(candidates) >= max_candidates:
                    break
            if not candidates:
                for script in soup.select('script[type="application/ld+json"]'):
                    if len(candidates) >= max_candidates:
                        break
                    try:
                        data = json.loads(script.get_text())
                    except (ValueError, TypeError):
                        continue
                    roots = data if isinstance(data, list) else [data]
                    for root in roots:
                        if len(candidates) >= max_candidates:
                            break
                        if not isinstance(root, dict):
                            continue
                        for item in [root, *root.get("@graph", [])] if isinstance(root.get("@graph", []), list) else [root]:
                            if not isinstance(item, dict):
                                continue
                            values = item.get("sameAs", [])
                            values = values if isinstance(values, list) else [values]
                            for url in [item.get("url"), *values]:
                                if len(candidates) >= max_candidates:
                                    break
                                if isinstance(url, str) and CAREER.search(url) and same_site(url, company.website):
                                    candidate = _save_candidate(db, company, url, str(item.get("name") or "官网结构化入口"), {
                                        "method": "official_metadata", "source_url": final,
                                        "page_sha256": digest, "observed_at": now().isoformat(),
                                    })
                                    if candidate and candidate.id not in {saved.id for saved in candidates}:
                                        candidates.append(candidate)
                            if len(candidates) >= max_candidates:
                                break
            status = "candidates_found" if candidates else "no_candidates"
            detail = "从已填官网发现候选；未读取候选站点、未自动绑定" if candidates else "已检查官网链接与结构化元数据，未发现可确认的同域招聘入口，可手工提供网址"
        elif config and config["enabled"]:
            query = f'"{company.name}" 招聘 官网'
            search = read_search_response(config["endpoint_url"], query, fetcher)
            for item in search.results:
                if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                    continue
                candidate = _save_candidate(db, company, item["url"], str(item.get("title") or "搜索候选"), {
                    **search.evidence, "method": "search_result", "snippet": str(item.get("content") or "")[:700],
                    "result_engines": [engine[:80] for engine in (item.get("engines") or [])[:10] if isinstance(engine, str)]
                        if isinstance(item.get("engines"), list) else [item["engine"][:80]] if isinstance(item.get("engine"), str) else [],
                    "observed_at": now().isoformat(), "identity_basis": "搜索候选，未核实企业主体；需要用户选择",
                })
                if candidate and candidate.id not in {saved.id for saved in candidates}:
                    candidates.append(candidate)
                if len(candidates) >= min(config["max_results"], max_candidates):
                    break
            status = "partial" if search.evidence["unresponsive_engines"] else "candidates_found" if candidates else "no_candidates"
            detail = ("部分引擎未响应，已保存可用候选供核对" if status == "partial" else
                      "搜索结果仅为候选，未访问或自动绑定任何候选站点" if candidates else
                      "本次查询未返回可用候选，请补充公司官网或调整名称")
        else:
            return {"status": "search_configuration_required", "company_id": str(company.id), "candidates": [],
                    "detail": "该公司尚未填写官网；可填写官网，或配置支持 JSON 输出的 SearXNG 搜索服务后按公司名发现入口",
                    "configuration": {"path": "/api/v1/settings/recruitment-discovery", "method": "PUT",
                                      "provider": "searxng", "fields": ["endpoint_url", "enabled", "max_results"]}}
        db.add(AuditEvent(user_id=actor_id, action="recruitment.entries_discovered", target=str(company.id),
                          detail={"status": status, "candidates": len(candidates),
                                  **({"search": search.evidence} if not company.website else {})}))
        return {"status": status, "company_id": str(company.id), "candidates": [row(c) for c in candidates[:max_candidates]],
                "detail": detail, **({"search": search.evidence} if not company.website else {})}
    finally:
        fetcher.close()


def bind_candidate(db, candidate, actor_id=None):
    if candidate.status == "rejected":
        raise CollectionError("candidate_rejected", "候选已被拒绝，不能直接绑定")
    company = db.get(Company, candidate.company_id)
    if not company or company.archived:
        raise CollectionError("company_archived", "公司不存在或已归档")
    kind = entry_source_kind(candidate.url)
    external_company_id = None
    if kind != "careers":
        from app.platform_collectors import expected_company_id
        external_company_id = expected_company_id(kind, {"entry_url": candidate.url})
        if not external_company_id:
            raise CollectionError("identity_review", "该平台链接不是可识别的公司页，请核对公司入口后在数据源中添加")
    validate_fetch_url(candidate.url)
    existing = db.scalar(select(SourceBinding).where(SourceBinding.company_id == candidate.company_id,
                                                     SourceBinding.kind == kind,
                                                     SourceBinding.entry_url == candidate.url))
    if not existing:
        existing = SourceBinding(company_id=candidate.company_id, kind=kind, entry_url=candidate.url,
                                 external_company_id=external_company_id,
                                 adapter="tencent" if urlparse(candidate.url).hostname == "careers.tencent.com" else "generic",
                                 config={"discovery_candidate_id": str(candidate.id)})
        db.add(existing)
        db.flush()
    candidate.kind, candidate.status, candidate.bound_source_id = kind, "bound", existing.id
    db.add(AuditEvent(user_id=actor_id, action="recruitment.entry_bound", target=str(candidate.id),
                      detail={"source_id": str(existing.id), "evidence": candidate.evidence}))
    return existing
