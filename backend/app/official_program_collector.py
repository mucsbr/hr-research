"""Checkpointed official-site collection whose parsing is performed by the frozen program."""
from copy import deepcopy
import json
import uuid
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
import httpx
from pydantic import ValidationError
from sqlalchemy import select

from app.adapter_code_package import content_sha256
from app.adapter_code_runtime import ProgramRuntime
from app.adapter_program import (
    capture_program_failure, prepare_program_input, validate_detail_output,
    validate_list_output, validate_navigation, rebind_official_detail_job,
)
from app.adapter_runtime_models import AdapterProgramExecution
from app.browser_capture import BrowserCapture
from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
from app.collection import CollectionError, JobDocument, PublicFetcher, require_readable_page, confirmed_end_marker
from app.config import get_settings
from app.db import session_factory
from app.llm import ModelError
from app.models import SourceRun, TaskRun
from app.official_checkpoint import CheckpointPage, digest as checkpoint_digest, plan_digest
from app.website_agent import WebsitePlan, declared_total, eligible_links, excerpt, get_text, plan_site


def _origin(url):
    try:
        value = urlparse(url)
        if value.scheme not in {"http", "https"} or not value.hostname or value.username or value.password:
            raise ValueError()
        return value.scheme, value.hostname.lower(), value.port or (443 if value.scheme == "https" else 80)
    except (TypeError, ValueError) as exc:
        raise CollectionError("source_host_mismatch", "官网网址不是可验证的同源HTTP地址") from exc


def _same_origin(url, entry):
    if _origin(url) != _origin(entry):
        raise CollectionError("source_host_mismatch", "官网页面或跳转超出绑定的同源范围")


class OriginPublicFetcher(PublicFetcher):
    """Retain PublicFetcher's DNS pinning and reject a cross-origin redirect before following it."""
    def __init__(self, entry, *, transport=None):
        super().__init__(transport=transport)
        self.entry = entry
        self.scope_guard = None

    def fetch(self, url):
        _same_origin(url, self.entry)
        host = urlparse(url).hostname
        if host not in self.clients:
            def response_guard(response):
                if self.scope_guard:
                    self.scope_guard()
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    original = response.request.url.copy_with(host=host,
                        port=urlparse(self.entry).port or (443 if urlparse(self.entry).scheme == "https" else 80))
                    _same_origin(urljoin(str(original), location), self.entry)
            self.clients[host] = httpx.Client(timeout=25, follow_redirects=False, trust_env=False,
                transport=self.transport, headers={"User-Agent": "HR-Intelligence/0.1 (public recruitment research)",
                                                    "Accept": "text/html,application/json"},
                event_hooks={"response": [response_guard]})
        return super().fetch(url)


class _BudgetStop(CollectionError):
    pass


class _Pages:
    def __init__(self, checkpoint, binding, filters, state, runtime):
        self.checkpoint, self.binding, self.state = checkpoint, binding, state
        self.runtime = runtime
        self.fetchers = {}
        settings = get_settings()
        requested = [filters.get("max_pages", 10), filters.get("max_details", 50)]
        if any(type(value) is not int or value < 1 for value in requested):
            raise CollectionError("invalid_budget", "官网列表与详情预算必须为正整数")
        self.limits = {"list": min(requested[0], settings.task_max_pages),
                       "detail": min(requested[1], settings.task_max_details)}
        self.used = {"list": 0, "detail": 0}
        with session_factory()() as db:
            checkpoint.guard_in_session(db)
            self.cached = set(db.scalars(select(OfficialCheckpointStep.request_key).where(
                OfficialCheckpointStep.checkpoint_id == checkpoint.id)))
            db.commit()

    def fetch(self, url, category, mode="http"):
        _same_origin(url, self.binding["entry_url"])
        self.runtime.check()
        self.checkpoint.assert_current()
        key = checkpoint_digest({"mode": mode, "url": url})
        cached = key in self.cached or key in self.checkpoint.pending
        if not cached:
            if self.used[category] >= self.limits[category]:
                raise _BudgetStop("official_page_budget", "达到本次" + ("列表" if category == "list" else "详情") + "读取预算，已保留检查点")
            self.used[category] += 1
        if mode not in self.fetchers:
            if mode == "browser":
                fetcher = BrowserCapture()
                if not hasattr(fetcher, "navigation_guard"):
                    fetcher.close()
                    raise CollectionError("browser_scope_guard_unavailable", "官网浏览器缺少同源导航守卫，未读取页面")
                fetcher.navigation_guard = lambda target: (self.runtime.check(), _same_origin(target, self.binding["entry_url"]))
                fetcher.check = self.checkpoint.assert_current
            else:
                fetcher = OriginPublicFetcher(self.binding["entry_url"])
                fetcher.scope_guard = self.runtime.check
            self.fetchers[mode] = fetcher
        page = self.checkpoint.fetch(self.fetchers[mode], url, mode=mode)
        self.runtime.check()
        _same_origin(page.final_url, self.binding["entry_url"])
        require_readable_page(page.body)
        if category == "list" and key not in self.state["list_keys"]:
            self.state["list_keys"].append(key)
            self.state["pages"] = len(self.state["list_keys"])
        if key not in self.cached:
            self.checkpoint.save(self.state, page=page, metadata={"parser": "official_program",
                "adapter_fingerprint": self.state["adapter_fingerprint"], "program_run_ids": []})
            self.cached.add(key)
        return page

    def close(self):
        for fetcher in self.fetchers.values():
            fetcher.close()


def _plan_binding(binding, checkpoint):
    saved = checkpoint.plan() or binding.get("config", {}).get("website_plan")
    if not saved:
        return deepcopy(binding), None
    raw = binding.get("config", {}).get("website_plan")
    if raw and plan_digest(raw) != binding.get("config", {}).get("plan_hash"):
        raise CollectionError("invalid_website_plan", "保存的官网计划与原始指纹不一致")
    try:
        plan = WebsitePlan.model_validate(saved)
    except ValidationError as exc:
        raise CollectionError("invalid_website_plan", "保存的官网计划不符合采集契约") from exc
    normalized = plan.model_dump()
    effective = deepcopy(binding)
    effective["config"] = {**binding.get("config", {}), "website_plan": normalized, "plan_hash": plan_digest(normalized)}
    return effective, plan


def _learn_plan(binding, identity, task_id, checkpoint, pages, listing):
    if checkpoint.records():
        raise CollectionError("checkpoint_rule_changed", "已有岗位后不能混合新旧网站计划，请新建明确范围任务")
    page, mode = listing, listing.mode
    candidates = eligible_links(BeautifulSoup(page.body, "html.parser"), "a[href]", page.final_url)
    candidates = [link for link in candidates if _origin(link) == _origin(binding["entry_url"])]
    if not candidates and mode == "http":
        page = pages.fetch(listing.requested_url, "list", "browser")
        mode = "browser"
        candidates = eligible_links(BeautifulSoup(page.body, "html.parser"), "a[href]", page.final_url)
        candidates = [link for link in candidates if _origin(link) == _origin(binding["entry_url"])]
    if not candidates:
        raise CollectionError("adaptation_required", "未观察到同源岗位详情入口，未调用网站计划模型")
    detail = pages.fetch(candidates[0], "detail", mode)
    # The model receives first-party sanitized DOM, never authenticated raw pages or navigation secrets.
    safe_list = prepare_program_input("official", binding, identity, operation="parse_list", page_url=page.final_url, html=page.body)
    safe_detail = prepare_program_input("official", binding, identity, operation="navigation", page_url=detail.final_url, html=detail.body)
    def safe_html(context):
        return next(doc["content"] for doc in context.request["documents"] if doc["type"] == "html")
    try:
        answer = plan_site({"task_id": str(task_id), "binding": binding, "filters": {}, "checkpoint": checkpoint,
                            "company_name": identity.get("name", ""), "render_mode": mode,
                            "listing_html": excerpt(safe_html(safe_list)), "detail_html": excerpt(safe_html(safe_detail))})
    except ModelError as exc:
        raise CollectionError(exc.code, exc.message) from exc
    plan = WebsitePlan.model_validate(answer["plan"])
    effective = deepcopy(binding)
    effective["config"] = {**binding.get("config", {}), "website_plan": plan.model_dump(),
                            "plan_hash": plan_digest(plan.model_dump())}
    return effective, plan, page


def _step_metadata(checkpoint, page):
    with session_factory()() as db:
        checkpoint.guard_in_session(db)
        step = db.scalar(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == checkpoint.id,
                                                             OfficialCheckpointStep.request_key == page.key))
        metadata = deepcopy(step.step_metadata) if step else {}
        db.commit()
        return metadata


def _inventory_evidence(context, page, effective, plan):
    """Coverage evidence is first-party, never a candidate's claimed count or completion flag."""
    controls = plan.model_dump() if plan else effective.get("config", {})
    soup = BeautifulSoup(page.body, "html.parser")
    try:
        total = declared_total(soup, controls.get("total_selector"))
        ended = confirmed_end_marker(get_text(soup, controls.get("complete_selector")) or "")
    except Exception as exc:
        raise CollectionError("invalid_website_plan", "官网总数或结束规则不是有效选择器") from exc
    jobs = sorted({link["url"] for link in context._links.values() if link["purpose"] == "job"})
    next_pages = sorted({link["url"] for link in context._links.values() if link["purpose"] == "next"})
    return {"jobs": jobs, "next_pages": next_pages, "declared_total": total, "ended": ended}


def _inherited_proof(checkpoint, descriptor, binding, identity, runtime, state):
    identifiers = set(state.get("program_run_ids", []))
    with session_factory()() as db:
        current = checkpoint.guard_in_session(db)
        task = db.get(TaskRun, checkpoint.task_id)
        allowed, cursor = {}, current
        for _ in range(32):
            owner = db.get(TaskRun, cursor.task_id)
            source = db.get(SourceRun, cursor.source_run_id)
            if (not owner or owner.company_id != task.company_id or not source or source.binding_id != current.binding_id
                    or cursor.scope_fingerprint != current.scope_fingerprint):
                raise CollectionError("checkpoint_code_provenance", "检查点接续链的公司、来源或范围不一致")
            allowed[owner.id] = source.id
            predecessor = cursor.state.get("resume_from_task_id")
            if not predecessor:
                break
            predecessor = uuid.UUID(str(predecessor))
            if predecessor in allowed:
                raise CollectionError("checkpoint_code_provenance", "检查点接续链出现循环")
            cursor = db.scalar(select(OfficialCheckpoint).where(OfficialCheckpoint.task_id == predecessor,
                                                               OfficialCheckpoint.binding_id == current.binding_id))
            if cursor is None:
                raise CollectionError("checkpoint_code_provenance", "原始接续检查点已丢失")
        else:
            raise CollectionError("checkpoint_code_provenance", "检查点接续链超过核验预算")
        steps = db.scalars(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == checkpoint.id)).all()
        for step in steps:
            if step.parsed_records:
                if step.step_metadata.get("adapter_fingerprint") != descriptor.fingerprint or not step.step_metadata.get("program_run_ids"):
                    raise CollectionError("checkpoint_code_provenance", "旧岗位没有绑定当前代码版本的执行证明")
            identifiers.update(step.step_metadata.get("program_run_ids", []))
        executions = db.scalars(select(AdapterProgramExecution).where(AdapterProgramExecution.remote_run_id.in_(identifiers))).all() if identifiers else []
        by_id = {}
        for item in executions:
            if (item.remote_run_id in by_id or item.status != "accepted" or item.task_id not in allowed
                    or item.source_run_id != allowed[item.task_id] or item.binding_id != current.binding_id
                    or item.adapter_fingerprint != descriptor.fingerprint or item.bundle_sha != runtime.reference["bundle_sha256"]
                    or item.request_sha != content_sha256(item.request) or item.input_sha != content_sha256(item.request.get("input"))):
                raise CollectionError("checkpoint_code_provenance", "旧代码执行不属于此明确接续链或没有通过第一方核验")
            output = runtime.client.verify_result(item.result, request=item.request,
                lock_sha256=runtime.reference["lock_sha256"], expected_runtime=runtime.reference["runtime_sha256"])
            if item.result["receipt"]["dependency_environment_sha256"] != runtime.reference["dependency_environment_sha256"]:
                raise CollectionError("checkpoint_code_provenance", "旧执行使用了其他依赖环境")
            by_id[item.remote_run_id] = (deepcopy(item.request["input"]), output)
        if set(by_id) != identifiers:
            raise CollectionError("checkpoint_code_provenance", "旧代码执行证明不完整")
        for step in steps:
            refs = step.step_metadata.get("program_run_ids", [])
            if not refs:
                continue
            _same_origin(step.final_url, binding["entry_url"])
            html = checkpoint._read_body(step)
            records = {value["source_job_id"]: JobDocument.model_validate(value) for value in step.parsed_records}
            validated = set()
            for run_id in refs:
                request, output = by_id[run_id]
                context_binding = deepcopy(binding)
                plan = request.get("plan")
                config = {key: value for key, value in binding.get("config", {}).items() if key not in {"website_plan", "plan_hash"}}
                if plan:
                    config.update(website_plan=plan, plan_hash=plan_digest(plan))
                context_binding["config"] = config
                operation = request["operation"]
                job = None
                if operation == "parse_detail":
                    identity_fields = request.get("job", {})
                    # Official URL-based native IDs are intentionally opaque in remote inputs.
                    # Match the reconstructed first-party request rather than assuming the wire id is the DB id.
                    candidates = [*records.values(), JobDocument(source_job_id=identity_fields["id"],
                                   title=identity_fields["title"], url=step.final_url)]
                    context = None
                    for candidate in candidates:
                        proposed = prepare_program_input("official", context_binding, identity, operation=operation,
                            page_url=step.final_url, html=html, job=candidate)
                        if content_sha256(proposed.request) == content_sha256(request):
                            job, context = candidate, proposed
                            break
                    if context is None:
                        raise CollectionError("checkpoint_code_provenance", "旧详情执行没有对应第一方岗位身份")
                else:
                    context = prepare_program_input("official", context_binding, identity, operation=operation,
                                                    page_url=step.final_url, html=html)
                if content_sha256(context.request) != content_sha256(request):
                    raise CollectionError("checkpoint_code_provenance", "旧执行输入与保存原文及计划不一致")
                if operation == "parse_detail":
                    document, _ = validate_detail_output(context, output)
                    if job.source_job_id in records:
                        saved = records[job.source_job_id]
                        if saved.observed_at != step.captured_at or saved.content_hash() != document.content_hash():
                            raise CollectionError("checkpoint_code_provenance", "旧岗位内容或观察时间与原始HTTP证据不同")
                        validated.add(saved.source_job_id)
                elif operation == "parse_list":
                    validate_list_output(context, output)
                else:
                    validate_navigation(context, output)
            if set(records) != validated:
                raise CollectionError("checkpoint_code_provenance", "旧岗位缺少完整详情代码执行证明")
        db.commit()
    return identifiers


def _collect(binding, identity, filters, task_id, source_run_id, expected_attempt,
             descriptor, checkpoint, progress=None):
    if checkpoint is None or descriptor.kind != "official" or binding.get("kind") != "careers":
        raise CollectionError("code_runtime_scope", "官网代码采集必须绑定官网来源和受控检查点")
    _origin(binding["entry_url"])
    state = checkpoint.cursor("official_program") or {
        "collector": "official_program", "adapter_fingerprint": descriptor.fingerprint,
        "pending": [binding["entry_url"]], "active": None, "seen_jobs": [], "seen_inputs": [], "list_keys": [],
        "pages": 0, "errors": [], "program_run_ids": [], "finished": False,
    }
    if state.get("adapter_fingerprint") != descriptor.fingerprint:
        raise CollectionError("checkpoint_code_changed", "代码版本已变化，不能接续旧程序的岗位和游标")
    for key in ("inventory_urls", "processed_urls", "unprocessed_next"):
        state.setdefault(key, [])
    state.setdefault("declared_total", None)
    state.setdefault("last_end", False)
    runtime = ProgramRuntime(descriptor, task_id=task_id, source_run_id=source_run_id,
        binding_snapshot=binding, expected_attempt=expected_attempt, scope_guard=checkpoint.guard_in_session)
    pages = _Pages(checkpoint, binding, filters, state, runtime)
    last_context = None
    inherited = set()

    def evidence(failed=False):
        current = runtime.evidence()
        combined = sorted(inherited | set(current["run_ids"]))
        return {**current, "run_ids": combined, "verified": bool(combined) and not failed and not runtime.errors}

    def finish(coverage, reason, finished=False):
        state.update(coverage=coverage, stop_reason=reason, finished=finished,
                     program_run_ids=sorted(inherited | set(runtime.evidence()["run_ids"])))
        result = checkpoint.result(state, coverage, reason, finished=finished)
        result.adapter_fingerprint, result.code_execution = descriptor.fingerprint, evidence()
        result.checkpoint_info = {**result.checkpoint_info, "site_plan_hash": state.get("plan_hash"),
                                  "inherited_run_ids": sorted(inherited), "new_list_requests": pages.used["list"],
                                  "new_detail_requests": pages.used["detail"]}
        return result

    def invoke(page, operation, effective, job=None):
        nonlocal last_context
        last_context = prepare_program_input("official", effective, identity, operation=operation,
                                              page_url=page.final_url, html=page.body, job=job)
        before = set(runtime.evidence()["run_ids"])
        parsed = runtime.invoke(last_context)
        metadata = _step_metadata(checkpoint, page)
        metadata.update(parser="official_program", adapter_fingerprint=descriptor.fingerprint,
            program_run_ids=sorted(set(metadata.get("program_run_ids", [])) | (set(runtime.evidence()["run_ids"]) - before)))
        state["program_run_ids"] = sorted(inherited | set(runtime.evidence()["run_ids"]))
        checkpoint.save(state, page=page, metadata=metadata)
        return parsed

    try:
        inherited = _inherited_proof(checkpoint, descriptor, binding, identity, runtime, state)
        effective, plan = _plan_binding(binding, checkpoint)
        state["plan_hash"] = effective.get("config", {}).get("plan_hash")
        if state.get("finished"):
            return finish(state["coverage"], state["stop_reason"], True)
        new_records = 0
        while state["active"] or state["pending"]:
            if state["active"] is None:
                url = state["pending"].pop(0)
                state["active"] = {"url": url, "mode": plan.render_mode if plan else "http", "stage": "list", "index": 0}
                checkpoint.save(state)
            active = state["active"]
            listing = pages.fetch(active["url"], "list", active["mode"])
            if active["stage"] == "list":
                probe = prepare_program_input("official", effective, identity, operation="parse_list",
                                               page_url=listing.final_url, html=listing.body)
                if not plan and not any(link["purpose"] == "job" for link in probe.request["links"]):
                    empty_proof = _inventory_evidence(probe, listing, effective, plan)
                    if empty_proof["declared_total"] != 0 and not empty_proof["ended"]:
                        effective, plan, listing = _learn_plan(binding, identity, task_id, checkpoint, pages, listing)
                        active["mode"], state["plan_hash"] = plan.render_mode, plan_digest(plan.model_dump())
                        checkpoint.save(state)
                parsed = invoke(listing, "parse_list", effective)
                inventory = _inventory_evidence(last_context, listing, effective, plan)
                active["inventory"] = inventory
                state["inventory_urls"] = sorted(set(state["inventory_urls"]) | set(inventory["jobs"]))
                if inventory["declared_total"] is not None:
                    if state["declared_total"] is not None and state["declared_total"] != inventory["declared_total"]:
                        state["errors"].append("采集期间第一方声明岗位总数变化")
                    state["declared_total"] = inventory["declared_total"]
                missing = set(inventory["jobs"]) - set(parsed.job_navigations.values())
                if missing:
                    state["errors"].append("代码列表未覆盖全部第一方已观察岗位链接")
                proved_empty = not inventory["jobs"] and (inventory["declared_total"] == 0 or inventory["ended"])
                if not parsed.records and not proved_empty:
                    code = "identity_review" if parsed.rejected_company_count else "source_contract_changed"
                    raise CollectionError(code, "官网代码未返回可核验的本公司岗位，没有将其当作零招聘")
                active["jobs"] = [{"document": doc.model_dump(mode="json"), "url": parsed.job_navigations[doc.source_job_id],
                                   "id_basis": parsed.identity_bases[doc.source_job_id]}
                                  for doc in parsed.records]
                active["stage"] = "details"
                checkpoint.save(state)
            while active["index"] < len(active["jobs"]):
                item = active["jobs"][active["index"]]
                job = JobDocument.model_validate(item["document"])
                original_id = job.source_job_id
                if original_id in state["seen_jobs"] or original_id in state.get("seen_inputs", []):
                    state["processed_urls"] = sorted(set(state["processed_urls"]) | {item["url"]})
                    active["index"] += 1
                    checkpoint.save(state)
                    continue
                if new_records >= pages.limits["detail"]:
                    return finish("partial", "达到本次岗位详情数量预算，已保存页内位置")
                detail = pages.fetch(item["url"], "detail", active["mode"])
                job = rebind_official_detail_job(effective, identity, job, requested_url=item["url"],
                    final_url=detail.final_url, html=detail.body, id_basis=item["id_basis"])
                document, _ = invoke(detail, "parse_detail", effective, job)
                document.raw_ref = detail.final_url
                accepted = checkpoint.records_for(detail)
                accepted = [old for old in accepted if old.source_job_id != document.source_job_id] + [document]
                state["seen_inputs"] = list(dict.fromkeys([*state.get("seen_inputs", []), original_id]))
                state["seen_jobs"] = list(dict.fromkeys([*state["seen_jobs"], document.source_job_id]))
                state["processed_urls"] = sorted(set(state["processed_urls"]) | {item["url"]})
                active["index"] += 1
                new_records += 1
                checkpoint.save(state, page=detail, records=accepted)
                if progress:
                    progress(f"官网代码已核验并保存 {len(state['seen_jobs'])} 个岗位详情")
            navigation = invoke(listing, "navigation", effective)
            followed = set()
            for action in navigation:
                if action.type == "follow":
                    followed.add(action.url)
                    _same_origin(action.url, binding["entry_url"])
                    key = checkpoint_digest({"mode": active["mode"], "url": action.url})
                    if key in state["list_keys"]:
                        state["errors"].append("已观察分页出现循环，未重复读取")
                    elif action.url not in state["pending"]:
                        state["pending"].append(action.url)
                elif action.type in {"scroll", "wait"}:
                    state["interactive_navigation_unresolved"] = True
            expected_next = set(active.get("inventory", {}).get("next_pages", []))
            missing_next = {url for url in expected_next if url not in followed
                            and checkpoint_digest({"mode": active["mode"], "url": url}) not in state["list_keys"]}
            state["unprocessed_next"] = sorted(set(state["unprocessed_next"]) | missing_next)
            if missing_next:
                state["errors"].append("代码导航未覆盖第一方已观察到的下一页")
            state["last_end"] = active.get("inventory", {}).get("ended", False)
            state["active"] = None
            checkpoint.save(state)
        inventory_complete = set(state["inventory_urls"]) <= set(state["processed_urls"])
        total = state.get("declared_total")
        positive_end = total == len(state["seen_jobs"]) if total is not None else state["last_end"]
        complete = inventory_complete and positive_end and not state["unprocessed_next"] and not state["errors"]
        if complete:
            return finish("complete", "第一方已核对全部观察岗位、分页/详情及明确总数或结束证据", True)
        reason = "已处理观察到的链接，尚无完整覆盖证明"
        if state.get("interactive_navigation_unresolved"):
            reason += "；程序提出的滚动/等待未作为已读取新页面"
        return finish("partial" if state["errors"] or state.get("interactive_navigation_unresolved") else "unknown", reason, True)
    except _BudgetStop as exc:
        if not evidence()["verified"]:
            raise CollectionError(exc.code, exc.message) from exc
        return finish("partial", exc.message)
    except CollectionError as exc:
        exc.code_execution = {**evidence(failed=True), "error_code": exc.code}
        exc.adapter_fingerprint = descriptor.fingerprint
        exc.repair_evidence = capture_program_failure(last_context) if last_context is not None else None
        raise
    finally:
        pages.close()


def collect_official_program(binding, identity, filters, task_id, source_run_id, expected_attempt,
                             descriptor, checkpoint, progress=None):
    try:
        return _collect(binding, identity, filters, task_id, source_run_id, expected_attempt,
                        descriptor, checkpoint, progress)
    except CollectionError as exc:
        if not hasattr(exc, "code_execution"):
            program = descriptor.program or {}
            exc.code_execution = {"release_id": descriptor.release_id, "release_generation": descriptor.generation,
                "fingerprint": descriptor.fingerprint, "bundle_sha256": program.get("bundle_sha256"),
                "lock_sha256": program.get("lock_sha256"), "runtime_sha256": program.get("runtime_sha256"),
                "verified": False, "run_ids": [], "error_code": exc.code}
        exc.adapter_fingerprint = descriptor.fingerprint
        if not hasattr(exc, "repair_evidence"):
            exc.repair_evidence = None
        raise


def capture_native_official_failure(binding, identity, checkpoint, error_code):
    """Bridge an actual native parsing failure from already-captured bytes; no HTTP, model or program run.

    The returned DTO is first-party input evidence only. It never marks native collection as verified code.
    A missing/ambiguous identity yields None so the original failure remains visible without invented oracles.
    """
    if checkpoint is None or binding.get("kind") != "careers" or error_code not in {
            "selector_mismatch", "source_contract_changed", "adaptation_required", "checkpoint_rule_changed"}:
        return None
    try:
        checkpoint.assert_current()
        effective, plan = _plan_binding(binding, checkpoint)
        cursor = checkpoint.cursor()
        pages = {page.key: page for page in checkpoint.pending.values()}
        with session_factory()() as db:
            checkpoint.guard_in_session(db)
            steps = db.scalars(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == checkpoint.id)
                               .order_by(OfficialCheckpointStep.captured_at.desc()).limit(20)).all()
            for step in steps:
                pages.setdefault(step.request_key, CheckpointPage(step.request_key, step.requested_url, step.final_url,
                    checkpoint._read_body(step), step.kind, step.captured_at))
            db.commit()
        observed = sorted(pages.values(), key=lambda page: page.captured_at, reverse=True)[:20]
        details = {}
        tencent_titles = {}
        for page in observed:
            _same_origin(page.final_url, binding["entry_url"])
            if binding.get("adapter") == "tencent":
                if urlparse(page.final_url).path.endswith("/Query"):
                    try:
                        payload = json.loads(page.body)
                        posts = payload.get("Data", {}).get("Posts", []) if payload.get("Code") == 200 else []
                        for item in posts[:100]:
                            if isinstance(item, dict) and item.get("PostId") is not None and isinstance(item.get("RecruitPostName"), str):
                                tencent_titles[str(item["PostId"])] = item["RecruitPostName"][:500]
                    except (ValueError, AttributeError, TypeError):
                        pass
                continue
            soup = BeautifulSoup(page.body, "html.parser")
            for url in eligible_links(soup, plan.job_link_selector if plan else "a[href]", page.final_url)[:100]:
                if _origin(url) != _origin(binding["entry_url"]):
                    continue
                anchor = next((node for node in soup.select("a[href]")
                               if urljoin(page.final_url, node.get("href", "")).split("#", 1)[0] == url), None)
                label = anchor.get_text(" ", strip=True) if anchor else ""
                if label and label not in {"详情", "查看详情", "查看", "申请", "立即申请", "了解更多"}:
                    details[url] = label[:500]
        listing_urls = {binding["entry_url"], cursor.get("next_page"), (cursor.get("active") or {}).get("url")}
        for page in observed:
            job = None
            operation = "parse_list"
            if binding.get("adapter") == "tencent":
                try:
                    payload = json.loads(page.body)
                except ValueError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if urlparse(page.final_url).path.endswith("/ByPostId"):
                    identifier = parse_qs(urlparse(page.final_url).query).get("postId", [None])[0]
                    data = payload.get("Data")
                    title = (data.get("RecruitPostName") if isinstance(data, dict) else None) or tencent_titles.get(identifier)
                    if not identifier or not isinstance(title, str) or not title.strip():
                        continue
                    job = JobDocument(source_job_id=identifier, title=title[:500],
                        url="https://careers.tencent.com/jobdesc.html?postId=" + identifier)
                    operation = "parse_detail"
                context = prepare_program_input("official", effective, identity, operation=operation,
                                                page_url=page.final_url, payload=payload, job=job)
            else:
                if page.requested_url in details and page.requested_url not in listing_urls:
                    title = get_text(BeautifulSoup(page.body, "html.parser"), plan.title_selector) if plan else None
                    job = JobDocument(source_job_id=page.final_url[:500], title=(title or details[page.requested_url])[:500],
                                      url=page.final_url)
                    job = rebind_official_detail_job(effective, identity, job, requested_url=page.requested_url,
                        final_url=page.final_url, html=page.body, id_basis="css")
                    operation = "parse_detail"
                context = prepare_program_input("official", effective, identity, operation=operation,
                                                page_url=page.final_url, html=page.body, job=job)
            safe = capture_program_failure(context)
            if safe is None:
                continue
            # A native collector may fail before committing its just-read page. Preserve those same bytes now.
            metadata = _step_metadata(checkpoint, page)
            checkpoint.save(cursor, page=page, metadata={**metadata, "native_failure_code": error_code})
            return safe
    except (CollectionError, KeyError, TypeError, ValueError):
        return None
    return None
