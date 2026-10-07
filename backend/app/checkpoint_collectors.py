"""Cursor-based official collectors. Existing collectors remain unchanged without a store."""
import json
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from app import collection as core
from app.config import get_settings
from app.official_checkpoint import plan_digest


def limits(filters):
    return (min(filters.get("max_pages", 10), get_settings().task_max_pages),
            min(filters.get("max_details", 50), get_settings().task_max_details))


def end_result(checkpoint, state, coverage, reason, finished=False):
    state.update(finished=finished, coverage=coverage, stop_reason=reason)
    return checkpoint.result(state, coverage, reason, finished=finished)


def previous_result(checkpoint, state):
    return checkpoint.result(state, state["coverage"], state["stop_reason"], finished=True)


def collect_generic(binding, filters, progress, checkpoint):
    state = checkpoint.cursor("generic") or {"collector": "generic", "pending": [binding["entry_url"]],
        "active": None, "visited": [], "seen": [], "pages": 0, "errors": [], "confirmed_end": False}
    if state.get("finished"):
        return previous_result(checkpoint, state)
    page_budget, record_budget = limits(filters)
    new_pages, new_records = 0, 0
    config = binding.get("config", {})
    fetcher = core.PublicFetcher()
    try:
        while state["active"] or state["pending"]:
            if state["active"] is None:
                if new_pages >= page_budget:
                    return end_result(checkpoint, state, "partial", "达到本次页面预算，已保存下一页检查点")
                url = state["pending"].pop(0)
                if url in state["visited"]:
                    continue
                page = checkpoint.fetch(fetcher, url)
                core.require_readable_page(page.body)
                soup = BeautifulSoup(page.body, "html.parser")
                core.jsonld_jobs(page.body, page.final_url)  # Verify extraction before committing the page.
                state["visited"].append(url)
                state["pages"] += 1
                new_pages += 1
                for selector in (config.get("job_link_selector"), config.get("next_selector")):
                    for node in soup.select(selector) if selector else []:
                        if not node.get("href"):
                            continue
                        target = urljoin(page.final_url, node["href"]).split("#", 1)[0]
                        parsed = urlparse(target)
                        if parsed.scheme not in {"http", "https"} or parsed.hostname != urlparse(page.final_url).hostname:
                            state["errors"].append("链接超出当前官网域名，未扩大采集范围")
                        elif target not in state["visited"] and target not in state["pending"]:
                            state["pending"].append(target)
                marker = soup.select_one(config["complete_selector"]) if config.get("complete_selector") else None
                state["confirmed_end"] = bool(marker and core.confirmed_end_marker(marker.get_text(" ", strip=True)))
                state["active"] = {"url": url, "index": 0}
                checkpoint.save(state, page=page, records=[], metadata={"parser": "generic"})
            active = state["active"]
            page = checkpoint.fetch(fetcher, active["url"])
            documents = core.jsonld_jobs(page.body, page.final_url)
            accepted = checkpoint.records_for(page)
            while active["index"] < len(documents):
                document = documents[active["index"]]
                if document.source_job_id not in state["seen"]:
                    if new_records >= record_budget:
                        return end_result(checkpoint, state, "partial", "达到本次岗位预算，已保存页内位置")
                    state["seen"].append(document.source_job_id)
                    accepted.append(document)
                    new_records += 1
                active["index"] += 1
                checkpoint.save(state, page=page, records=accepted)
                if progress:
                    progress(f"已验证 {state['pages']} 页，累计提取 {len(state['seen'])} 个岗位")
            state["active"] = None
            checkpoint.save(state)
        coverage = "complete" if state["confirmed_end"] and not state["errors"] else "unknown"
        if not state["seen"] and coverage != "complete":
            raise core.CollectionError("adaptation_required", "入口尚未识别到结构化岗位，需要官网适配")
        return end_result(checkpoint, state, coverage,
                          "已验证配置范围结束标记" if coverage == "complete" else "没有可验证的完整覆盖证据", True)
    finally:
        fetcher.close()


def collect_plan(binding, filters, plan, progress, checkpoint):
    from app.browser_capture import BrowserCapture
    from app.website_agent import declared_total, eligible_links, get_text, inline_jobs, next_page_url
    plan_hash = plan_digest(plan.model_dump())
    state = checkpoint.cursor("plan") or {"collector": "plan", "plan_hash": plan_hash,
        "next_page": binding["entry_url"], "active": None, "seen_pages": [], "seen_jobs": [],
        "pages": 0, "errors": [], "declared_total": None}
    if state["plan_hash"] != plan_hash:
        raise core.CollectionError("checkpoint_scope_changed", "官网规则指纹已变化，未复用旧进度")
    if state.get("finished"):
        return previous_result(checkpoint, state)
    if not state["active"] and not state["next_page"]:
        last = state.get("last_page", {})
        complete = not state["errors"] and (last.get("total") == len(state["seen_jobs"])
                    if last.get("total") is not None else last.get("end", False))
        return end_result(checkpoint, state, "complete" if complete else "unknown",
                          "已核对数量或结束标记" if complete else "没有可验证的完整覆盖证据", True)
    page_budget, detail_budget = limits(filters)
    new_pages, new_details = 0, 0
    fetcher = BrowserCapture() if plan.render_mode == "browser" else core.PublicFetcher()
    try:
        while state["active"] or state["next_page"]:
            if state["active"] is None:
                if new_pages >= page_budget:
                    return end_result(checkpoint, state, "partial", "达到本次列表页预算，已保存下一页")
                url = state["next_page"]
                if url in state["seen_pages"]:
                    return end_result(checkpoint, state, "partial", "分页重复，不能宣称完整覆盖", True)
                page = checkpoint.fetch(fetcher, url, mode=plan.render_mode)
                core.require_readable_page(page.body)
                soup = BeautifulSoup(page.body, "html.parser")
                inline = inline_jobs(soup, plan, page.final_url) if plan.job_row_selector else []
                links = ([document.url for document in inline] if plan.job_row_selector else
                         list(dict.fromkeys(eligible_links(soup, plan.job_link_selector, page.final_url))))
                total = declared_total(soup, plan.total_selector)
                if not links and not state["seen_jobs"] and total != 0:
                    raise core.CollectionError("selector_mismatch", "保存的岗位规则未匹配到岗位")
                if total is not None:
                    if state["declared_total"] is not None and state["declared_total"] != total:
                        state["errors"].append("采集期间列表总数发生变化，未视为完整快照")
                    state["declared_total"] = total
                next_url = next_page_url(soup, plan, page.final_url)
                if next_url and urlparse(next_url).hostname != urlparse(page.final_url).hostname:
                    next_url = None
                    state["errors"].append("分页跳转到其他域名，未自动扩大范围")
                state["seen_pages"].append(url)
                state["pages"] += 1
                new_pages += 1
                state["active"] = {"url": url, "links": links, "index": 0, "next_url": next_url,
                    "total": total, "end": core.confirmed_end_marker(get_text(soup, plan.complete_selector) or "")}
                checkpoint.save(state, page=page, records=[], metadata={"parser": "plan", "plan_hash": plan_hash})
            active = state["active"]
            while active["index"] < len(active["links"]):
                link = active["links"][active["index"]]
                if link in state["seen_jobs"]:
                    active["index"] += 1
                    checkpoint.save(state)
                    continue
                if new_details >= detail_budget:
                    return end_result(checkpoint, state, "partial", "达到本次详情预算，已保存页内详情位置")
                page = checkpoint.fetch(fetcher, active["url"] if plan.job_row_selector else link, mode=plan.render_mode)
                core.require_readable_page(page.body)
                structured = [] if plan.job_row_selector else core.jsonld_jobs(page.body, page.final_url)
                if plan.job_row_selector:
                    documents = inline_jobs(BeautifulSoup(page.body, "html.parser"), plan, page.final_url)
                    document = next((item for item in documents if item.url == link), None)
                    if document is None:
                        raise core.CollectionError("selector_mismatch", "列表岗位与已保存的页内位置不一致")
                elif structured:
                    document = structured[0]
                else:
                    detail = BeautifulSoup(page.body, "html.parser")
                    title = get_text(detail, plan.title_selector)
                    description = get_text(detail, plan.description_selector)
                    if not title or not description:
                        raise core.CollectionError("selector_mismatch", "岗位标题或职责规则失效，需要重新适配")
                    location = get_text(detail, plan.location_selector)
                    document = core.JobDocument(source_job_id=page.final_url[:500], title=title[:500], url=page.final_url,
                        description=description, requirements=get_text(detail, plan.requirements_selector) or "",
                        locations=[location] if location else [], salary_raw=get_text(detail, plan.salary_selector), raw_ref=page.final_url)
                state["seen_jobs"].append(link)
                active["index"] += 1
                new_details += 1
                accepted = checkpoint.records_for(page) if plan.job_row_selector else []
                checkpoint.save(state, page=page, records=[*accepted, document], metadata={"parser": "plan", "plan_hash": plan_hash})
                if progress:
                    progress(f"官网检查点已保存 {len(state['seen_jobs'])} 个岗位")
            state["next_page"] = active["next_url"]
            state["last_page"] = {"total": active["total"], "end": active["end"]}
            state["active"] = None
            checkpoint.save(state)
            if not state["next_page"]:
                complete = not state["errors"] and (active["total"] == len(state["seen_jobs"])
                            if active["total"] is not None else active["end"])
                return end_result(checkpoint, state, "complete" if complete else "unknown",
                                  "已核对数量或结束标记" if complete else "没有可验证的完整覆盖证据", True)
    finally:
        fetcher.close()


def collect_tencent(binding, filters, progress, checkpoint):
    if urlparse(binding["entry_url"]).hostname != "careers.tencent.com":
        raise core.CollectionError("adapter_host_mismatch", "腾讯适配器只能绑定腾讯官方招聘域名")
    state = checkpoint.cursor("tencent") or {"collector": "tencent", "page_index": 1,
        "active": None, "seen": [], "pages": 0, "errors": [], "total": None}
    if state.get("finished"):
        return previous_result(checkpoint, state)
    if state["active"] is None and state["total"] is not None and len(state["seen"]) >= state["total"]:
        return end_result(checkpoint, state, "complete" if not state["errors"] else "partial",
                          "已遍历来源列表" if not state["errors"] else "来源总数变化，保留部分覆盖", True)
    page_budget, detail_budget = limits(filters)
    new_pages, new_details = 0, 0
    fetcher = core.PublicFetcher()
    try:
        while True:
            if state["active"] is None:
                if new_pages >= page_budget:
                    return end_result(checkpoint, state, "partial", "达到本次列表页预算，已保存腾讯页码")
                query = urlencode({"pageIndex": state["page_index"], "pageSize": min(10, detail_budget),
                                   "language": "zh-cn", "area": "cn"})
                page = checkpoint.fetch(fetcher, "https://careers.tencent.com/tencentcareer/api/post/Query?" + query)
                try:
                    payload = json.loads(page.body)
                    data = payload.get("Data")
                    if payload.get("Code") != 200 or not isinstance(data, dict) or not isinstance(data.get("Posts"), list):
                        raise ValueError()
                except (ValueError, AttributeError):
                    raise core.CollectionError("source_contract_changed", "腾讯列表响应未通过结构验证") from None
                total = data.get("Count")
                total = total if type(total) is int and total >= 0 else None
                if state["total"] is not None and total is not None and state["total"] != total:
                    state["errors"].append("采集期间腾讯列表总数变化，未视为完整快照")
                state["total"] = total
                post_ids = list(dict.fromkeys(str(post.get("PostId")) for post in data["Posts"]
                                             if isinstance(post, dict) and post.get("PostId")))
                state["pages"] += 1
                new_pages += 1
                state["active"] = {"url": page.requested_url, "posts": post_ids, "index": 0}
                checkpoint.save(state, page=page, records=[], metadata={"parser": "tencent"})
                if not post_ids:
                    complete = total == 0 and state["page_index"] == 1 and not state["errors"]
                    return end_result(checkpoint, state, "complete" if complete else "unknown", "来源返回空列表", True)
            active = state["active"]
            if not active["posts"]:
                complete = state["total"] == 0 and state["page_index"] == 1 and not state["errors"]
                return end_result(checkpoint, state, "complete" if complete else "unknown", "来源返回空列表", True)
            while active["index"] < len(active["posts"]):
                key = active["posts"][active["index"]]
                if key in state["seen"]:
                    active["index"] += 1
                    checkpoint.save(state)
                    continue
                if new_details >= detail_budget:
                    return end_result(checkpoint, state, "partial", "达到本次腾讯详情预算，已保存待处理岗位")
                page = checkpoint.fetch(fetcher, "https://careers.tencent.com/tencentcareer/api/post/ByPostId?" + urlencode({"postId": key, "language": "zh-cn"}))
                try:
                    response = json.loads(page.body)
                    detail = response.get("Data")
                    if response.get("Code") != 200 or not isinstance(detail, dict) or not detail.get("RecruitPostName"):
                        raise ValueError()
                except (ValueError, AttributeError):
                    raise core.CollectionError("source_contract_changed", "腾讯岗位详情响应未通过验证，检查点未跳过该岗位") from None
                document = core.JobDocument(source_job_id=key, title=detail["RecruitPostName"],
                    url="https://careers.tencent.com/jobdesc.html?postId=" + key,
                    locations=[detail.get("LocationName", "")], department=detail.get("BGName"), function=detail.get("CategoryName"),
                    experience=detail.get("RequireWorkYearsName"), recruiting_entity=detail.get("ComName"),
                    description=core.text_value(detail.get("Responsibility")), requirements=core.text_value(detail.get("Requirement")),
                    source_updated_at=detail.get("LastUpdateTime"), raw_ref=page.final_url)
                active["index"] += 1
                state["seen"].append(key)
                new_details += 1
                checkpoint.save(state, page=page, records=[document], metadata={"parser": "tencent"})
                if progress:
                    progress(f"腾讯官网检查点已保存 {len(state['seen'])} 个岗位")
            state["active"] = None
            state["page_index"] += 1
            checkpoint.save(state)
            if state["total"] is not None and len(state["seen"]) >= state["total"]:
                return end_result(checkpoint, state, "complete" if not state["errors"] else "partial",
                                  "已遍历来源列表" if not state["errors"] else "来源总数变化，保留部分覆盖", True)
    finally:
        fetcher.close()
