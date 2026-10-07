"""Fixed Tencent GET transport; every job field is parsed by the frozen remote program."""

from copy import deepcopy
import json
import re
import uuid
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx
from sqlalchemy import select

from app.adapter_code_package import content_sha256
from app.adapter_code_runtime import ProgramRuntime
from app.adapter_program import (
    capture_program_failure,
    prepare_program_input,
    validate_detail_output,
    validate_list_output,
)
from app.adapter_runtime_models import AdapterProgramExecution
from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
from app.collection import CollectionError, JobDocument, PublicFetcher
from app.config import get_settings
from app.db import session_factory
from app.models import SourceRun, TaskRun
from app.official_checkpoint import digest as checkpoint_digest

ORIGIN = "https://careers.tencent.com"
QUERY = "/tencentcareer/api/post/Query"
DETAIL = "/tencentcareer/api/post/ByPostId"


def _post_id(value):
    if type(value) not in {str, int} or not re.fullmatch(r"[0-9]{1,30}", str(value)):
        raise CollectionError("source_contract_changed", "腾讯岗位编号缺少可验证的原始数字ID")
    return str(value)


def _api_signature(url):
    try:
        parsed = urlsplit(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(pairs)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "careers.tencent.com"
            or parsed.port not in {None, 443}
            or parsed.username
            or parsed.password
            or parsed.fragment
            or len(pairs) != len(query)
        ):
            raise ValueError()
        if parsed.path == QUERY:
            if (
                set(query) != {"pageIndex", "pageSize", "language", "area"}
                or query["language"] != "zh-cn"
                or query["area"] != "cn"
                or not re.fullmatch(r"[1-9][0-9]{0,5}", query["pageIndex"])
                or not re.fullmatch(r"[1-9]|10", query["pageSize"])
            ):
                raise ValueError()
        elif parsed.path == DETAIL:
            if set(query) != {"postId", "language"} or query["language"] != "zh-cn":
                raise ValueError()
            _post_id(query["postId"])
        else:
            raise ValueError()
        return parsed.path, tuple(sorted(query.items()))
    except (ValueError, TypeError, CollectionError) as exc:
        raise CollectionError(
            "source_host_mismatch", "腾讯代码采集仅允许两个已登记GET接口及固定参数"
        ) from exc


class TencentProgramFetcher(PublicFetcher):
    """Reject changed origin, endpoint, page or PostId before following any redirect."""

    def fetch(self, url):
        expected = _api_signature(url)
        self.expected_url, self.expected_signature = url, expected
        if "careers.tencent.com" not in self.clients:

            def redirect_guard(response):
                if response.status_code in {301, 302, 303, 307, 308}:
                    target = urljoin(self.expected_url, response.headers.get("location", ""))
                    if _api_signature(target) != self.expected_signature:
                        raise CollectionError(
                            "source_host_mismatch", "腾讯接口重定向改变了原请求范围"
                        )

            self.clients["careers.tencent.com"] = httpx.Client(
                timeout=25,
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
                headers={
                    "User-Agent": "HR-Intelligence/0.1 (public recruitment research)",
                    "Accept": "application/json",
                },
                event_hooks={"response": [redirect_guard]},
            )
        final, body = super().fetch(url)
        if _api_signature(final) != expected:
            raise CollectionError("source_host_mismatch", "腾讯响应不属于原固定接口请求")
        return final, body


def _payload(page, *, post_id=None):
    try:
        value = json.loads(page.body)
        if not isinstance(value, dict) or type(value.get("Code")) is not int:
            raise ValueError()
        if value["Code"] in {401, 403, 429}:
            raise CollectionError(
                "access_restricted", "腾讯接口要求访问授权或限制请求，未运行解析代码"
            )
        data = value.get("Data")
        if value["Code"] != 200 or not isinstance(data, dict):
            raise ValueError()
        if post_id is None:
            posts = data.get("Posts")
            if (
                not isinstance(posts, list)
                or len(posts) > 100
                or any(not isinstance(post, dict) for post in posts)
            ):
                raise ValueError()
            ids = [_post_id(post.get("PostId")) for post in posts]
            if len(ids) != len(set(ids)):
                raise ValueError()
        elif _post_id(data.get("PostId")) != post_id:
            raise CollectionError("identity_review", "腾讯详情响应岗位ID与原列表请求不一致")
        return value
    except (ValueError, TypeError, AttributeError) as exc:
        raise CollectionError(
            "source_contract_changed", "腾讯接口Code或岗位数据结构不符合登记契约"
        ) from exc


def _metadata(checkpoint, page):
    with session_factory()() as db:
        checkpoint.guard_in_session(db)
        step = db.scalar(
            select(OfficialCheckpointStep).where(
                OfficialCheckpointStep.checkpoint_id == checkpoint.id,
                OfficialCheckpointStep.request_key == page.key,
            )
        )
        value = deepcopy(step.step_metadata) if step else {}
        db.commit()
        return value


def _document_content(document):
    return document.model_dump(mode="json", exclude={"observed_at", "raw_ref"})


def _lineage(db, checkpoint, current):
    task = db.get(TaskRun, checkpoint.task_id)
    allowed, cursor = {}, current
    for _ in range(32):
        owner = db.get(TaskRun, cursor.task_id)
        source = db.get(SourceRun, cursor.source_run_id)
        if (
            not owner
            or owner.company_id != task.company_id
            or not source
            or source.binding_id != current.binding_id
            or cursor.scope_fingerprint != current.scope_fingerprint
        ):
            raise CollectionError(
                "checkpoint_code_provenance", "腾讯接续链不属于同公司、来源与范围"
            )
        if owner.id in allowed:
            raise CollectionError("checkpoint_code_provenance", "腾讯检查点接续链出现循环")
        allowed[owner.id] = source.id
        predecessor = cursor.state.get("resume_from_task_id")
        if not predecessor:
            return allowed
        cursor = db.scalar(
            select(OfficialCheckpoint).where(
                OfficialCheckpoint.task_id == uuid.UUID(predecessor),
                OfficialCheckpoint.binding_id == current.binding_id,
            )
        )
        if cursor is None:
            break
    raise CollectionError("checkpoint_code_provenance", "腾讯原始接续链缺失或超过核验预算")


def _inherit(checkpoint, descriptor, binding, identity, runtime, state):
    """Reconstruct inputs from saved API bytes; never relabel native or other-code records."""
    identifiers = set(state.get("program_run_ids", []))
    lists = {}
    with session_factory()() as db:
        current = checkpoint.guard_in_session(db)
        allowed = _lineage(db, checkpoint, current)
        steps = db.scalars(
            select(OfficialCheckpointStep).where(
                OfficialCheckpointStep.checkpoint_id == checkpoint.id
            )
        ).all()
        for step in steps:
            _api_signature(step.requested_url)
            if _api_signature(step.final_url) != _api_signature(step.requested_url):
                raise CollectionError("checkpoint_code_provenance", "腾讯旧HTTP证据改变了请求范围")
            refs = step.step_metadata.get("program_run_ids", [])
            if step.parsed_records or refs:
                if (
                    step.step_metadata.get("parser") != "tencent_program"
                    or step.step_metadata.get("adapter_fingerprint") != descriptor.fingerprint
                    or not refs
                ):
                    raise CollectionError(
                        "checkpoint_code_provenance", "腾讯旧证据没有匹配当前代码版本"
                    )
            identifiers.update(refs)
        executions = (
            db.scalars(
                select(AdapterProgramExecution).where(
                    AdapterProgramExecution.remote_run_id.in_(identifiers)
                )
            ).all()
            if identifiers
            else []
        )
        evidence = {}
        for item in executions:
            if (
                item.remote_run_id in evidence
                or item.status != "accepted"
                or item.task_id not in allowed
                or item.source_run_id != allowed[item.task_id]
                or item.binding_id != checkpoint.binding_id
                or item.adapter_fingerprint != descriptor.fingerprint
                or item.bundle_sha != runtime.reference["bundle_sha256"]
                or item.request_sha != content_sha256(item.request)
                or item.input_sha != content_sha256(item.request.get("input"))
            ):
                raise CollectionError(
                    "checkpoint_code_provenance", "腾讯旧运行证明不属于明确的已核验接续链"
                )
            output = runtime.client.verify_result(
                item.result,
                request=item.request,
                lock_sha256=runtime.reference["lock_sha256"],
                expected_runtime=runtime.reference["runtime_sha256"],
            )
            if (
                item.result["receipt"]["dependency_environment_sha256"]
                != runtime.reference["dependency_environment_sha256"]
            ):
                raise CollectionError("checkpoint_code_provenance", "腾讯旧运行使用了其他依赖环境")
            evidence[item.remote_run_id] = (deepcopy(item.request["input"]), output)
        if set(evidence) != identifiers:
            raise CollectionError("checkpoint_code_provenance", "腾讯旧程序回执不完整")
        records_seen = set()
        for step in sorted(steps, key=lambda item: urlsplit(item.final_url).path != QUERY):
            refs = step.step_metadata.get("program_run_ids", [])
            if not refs:
                continue
            listing = urlsplit(step.final_url).path == QUERY
            key = None if listing else dict(parse_qsl(urlsplit(step.final_url).query))["postId"]
            payload = _payload(
                type("Page", (), {"body": checkpoint._read_body(step)})(), post_id=key
            )
            job = None
            if not listing:
                saved_job = step.step_metadata.get("input_job")
                list_url = step.step_metadata.get("list_url")
                parsed_list = lists.get(checkpoint_digest({"mode": "http", "url": list_url}))
                job = JobDocument.model_validate(saved_job) if saved_job else None
                original = (
                    next((doc for doc in parsed_list.records if doc.source_job_id == key), None)
                    if parsed_list
                    else None
                )
                if (
                    not job
                    or not original
                    or job.source_job_id != key
                    or _document_content(job) != _document_content(original)
                    or job.url != original.url
                ):
                    raise CollectionError(
                        "checkpoint_code_provenance", "腾讯旧详情未绑定原列表的已核验岗位"
                    )
            context = prepare_program_input(
                "official",
                binding,
                identity,
                operation="parse_list" if listing else "parse_detail",
                page_url=step.final_url,
                payload=payload,
                job=job,
            )
            parsed = None
            for ref in refs:
                request, output = evidence[ref]
                if content_sha256(context.request) != content_sha256(request):
                    raise CollectionError(
                        "checkpoint_code_provenance", "腾讯旧输入与保存API原文不一致"
                    )
                parsed = (
                    validate_list_output(context, output)
                    if listing
                    else validate_detail_output(context, output)[0]
                )
            if listing:
                if step.parsed_records or parsed.raw_count != len(payload["Data"]["Posts"]):
                    raise CollectionError(
                        "checkpoint_code_provenance", "腾讯列表证明遗漏原始岗位或含伪造详情"
                    )
                lists[step.request_key] = parsed
            else:
                saved = [JobDocument.model_validate(value) for value in step.parsed_records]
                if saved and (
                    len(saved) != 1
                    or saved[0].source_job_id != parsed.source_job_id
                    or _document_content(saved[0]) != _document_content(parsed)
                    or saved[0].observed_at != step.captured_at
                    or saved[0].raw_ref != step.final_url
                ):
                    raise CollectionError(
                        "checkpoint_code_provenance", "腾讯旧岗位内容或观察时间与原始详情证据不符"
                    )
                records_seen.update(doc.source_job_id for doc in saved)
        if records_seen != set(state.get("seen", [])):
            raise CollectionError(
                "checkpoint_code_provenance", "腾讯游标已完成岗位与原始详情证明不一致"
            )
        if (
            len(state.get("seen", [])) != len(records_seen)
            or set(state.get("list_keys", [])) != set(lists)
            or len(state.get("list_keys", [])) != len(lists)
            or state.get("pages", 0) != len(lists)
        ):
            raise CollectionError(
                "checkpoint_code_provenance", "腾讯页数或岗位游标重复、缺失原始代码证明"
            )
        committed_raw = set()
        active = state.get("active") or {}
        for step in steps:
            if step.request_key in lists and not (
                active.get("stage") == "list" and active.get("url") == step.requested_url
            ):
                payload = _payload(type("Page", (), {"body": checkpoint._read_body(step)})())
                committed_raw.update(_post_id(row["PostId"]) for row in payload["Data"]["Posts"])
        if committed_raw != set(state.get("raw_seen", [])) or len(state.get("raw_seen", [])) != len(
            committed_raw
        ):
            raise CollectionError(
                "checkpoint_code_provenance", "腾讯完整覆盖计数没有原始岗位ID依据"
            )
        db.commit()
    return identifiers, lists


def collect_tencent_program(
    binding,
    identity,
    filters,
    task_id,
    source_run_id,
    expected_attempt,
    descriptor,
    checkpoint,
    progress=None,
):
    try:
        entry = urlsplit(binding.get("entry_url", ""))
        valid_origin = (
            entry.scheme == "https"
            and entry.hostname == "careers.tencent.com"
            and entry.port in {None, 443}
            and not entry.username
            and not entry.password
        )
    except (ValueError, TypeError):
        valid_origin = False
    if (
        checkpoint is None
        or descriptor.kind != "official"
        or binding.get("kind") != "careers"
        or binding.get("adapter") != "tencent"
        or not valid_origin
    ):
        raise CollectionError(
            "code_runtime_scope", "腾讯代码采集必须绑定已登记官网、代码版本和受控检查点"
        )
    settings = get_settings()
    budgets = [
        filters.get("max_pages", 10),
        filters.get("max_details", 50),
        settings.task_max_pages,
        settings.task_max_details,
    ]
    if any(type(value) is not int or value < 1 for value in budgets):
        raise CollectionError("invalid_budget", "腾讯列表与详情预算必须为正整数")
    page_limit, detail_limit = min(budgets[0], budgets[2]), min(budgets[1], budgets[3])
    state = checkpoint.cursor("tencent_program") or {
        "collector": "tencent_program",
        "adapter_fingerprint": descriptor.fingerprint,
        "page_index": 1,
        "page_size": min(10, detail_limit),
        "active": None,
        "seen": [],
        "raw_seen": [],
        "list_keys": [],
        "pages": 0,
        "errors": [],
        "total": None,
        "program_run_ids": [],
        "finished": False,
    }
    if state.get("adapter_fingerprint") != descriptor.fingerprint:
        raise CollectionError("checkpoint_code_changed", "腾讯代码版本已变化，不能接续旧程序游标")
    runtime = ProgramRuntime(
        descriptor,
        task_id=task_id,
        source_run_id=source_run_id,
        binding_snapshot=binding,
        expected_attempt=expected_attempt,
        scope_guard=checkpoint.guard_in_session,
    )
    fetcher, inherited, cached_lists, last_context = TencentProgramFetcher(), set(), {}, None
    used = {"list": 0, "detail": 0}
    with session_factory()() as db:
        checkpoint.guard_in_session(db)
        cached = set(
            db.scalars(
                select(OfficialCheckpointStep.request_key).where(
                    OfficialCheckpointStep.checkpoint_id == checkpoint.id
                )
            )
        )
        db.commit()

    def proof(failed=False):
        evidence = runtime.evidence()
        ids = sorted(inherited | set(evidence["run_ids"]))
        return {
            **evidence,
            "run_ids": ids,
            "verified": bool(ids) and not failed and not runtime.errors,
        }

    def finish(coverage, reason, finished=False):
        state.update(
            coverage=coverage,
            stop_reason=reason,
            finished=finished,
            program_run_ids=proof()["run_ids"],
        )
        result = checkpoint.result(state, coverage, reason, finished=finished)
        result.adapter_fingerprint, result.code_execution = descriptor.fingerprint, proof()
        result.checkpoint_info = {
            **result.checkpoint_info,
            "inherited_run_ids": sorted(inherited),
            "new_list_requests": used["list"],
            "new_detail_requests": used["detail"],
        }
        return result

    def fetch(url, category):
        nonlocal last_context
        last_context = None
        expected = _api_signature(url)
        checkpoint.assert_current()
        key = checkpoint_digest({"mode": "http", "url": url})
        if key not in cached and key not in checkpoint.pending:
            if used[category] >= (page_limit if category == "list" else detail_limit):
                return None
            used[category] += 1
        page = checkpoint.fetch(fetcher, url)
        if _api_signature(page.final_url) != expected:
            raise CollectionError("source_host_mismatch", "腾讯HTTP证据改变原请求范围")
        _payload(page, post_id=dict(expected[1])["postId"] if category == "detail" else None)
        if key not in cached:
            checkpoint.save(
                state,
                page=page,
                metadata={
                    "parser": "tencent_program",
                    "adapter_fingerprint": descriptor.fingerprint,
                    "program_run_ids": [],
                },
            )
            cached.add(key)
        return page

    def invoke(page, payload, operation, job=None, list_url=None):
        nonlocal last_context
        last_context = prepare_program_input(
            "official",
            binding,
            identity,
            operation=operation,
            page_url=page.final_url,
            payload=payload,
            job=job,
        )
        parsed = runtime.invoke(last_context)
        with session_factory()() as db:
            checkpoint.guard_in_session(db)
            ids = set(
                db.scalars(
                    select(AdapterProgramExecution.remote_run_id).where(
                        AdapterProgramExecution.task_id == uuid.UUID(str(task_id)),
                        AdapterProgramExecution.source_run_id == uuid.UUID(str(source_run_id)),
                        AdapterProgramExecution.input_sha == content_sha256(last_context.request),
                        AdapterProgramExecution.operation == operation,
                        AdapterProgramExecution.status == "accepted",
                        AdapterProgramExecution.bundle_sha == runtime.reference["bundle_sha256"],
                        AdapterProgramExecution.adapter_fingerprint == descriptor.fingerprint,
                    )
                )
            ) & set(runtime.evidence()["run_ids"])
            db.commit()
        if not ids:
            raise CollectionError(
                "checkpoint_code_provenance", "腾讯解析没有持久化的第一方核验回执"
            )
        metadata = _metadata(checkpoint, page)
        metadata.update(
            parser="tencent_program",
            adapter_fingerprint=descriptor.fingerprint,
            program_run_ids=sorted(set(metadata.get("program_run_ids", [])) | ids),
        )
        if job is not None:
            metadata.update(input_job=job.model_dump(mode="json"), list_url=list_url)
        state["program_run_ids"] = proof()["run_ids"]
        if operation == "parse_list" and page.key not in state["list_keys"]:
            state["list_keys"].append(page.key)
            state["pages"] = len(state["list_keys"])
        checkpoint.save(state, page=page, metadata=metadata)
        return parsed

    try:
        inherited, cached_lists = _inherit(
            checkpoint, descriptor, binding, identity, runtime, state
        )
        if state["finished"]:
            return finish(state["coverage"], state["stop_reason"], True)
        new_records = 0
        while True:
            if state["active"] is None:
                if used["list"] >= page_limit:
                    return finish("partial", "达到本次腾讯列表预算，已保存pageIndex")
                query = urlencode(
                    {
                        "pageIndex": state["page_index"],
                        "pageSize": state["page_size"],
                        "language": "zh-cn",
                        "area": "cn",
                    }
                )
                state["active"] = {
                    "url": ORIGIN + QUERY + "?" + query,
                    "stage": "list",
                    "posts": [],
                    "index": 0,
                }
                checkpoint.save(state)
            active = state["active"]
            listing = fetch(active["url"], "list")
            if listing is None:
                return finish("partial", "达到本次腾讯列表预算，已保存pageIndex")
            payload = _payload(listing)
            parsed = cached_lists.get(listing.key)
            if parsed is None:
                parsed = invoke(listing, payload, "parse_list")
                if parsed.raw_count != len(payload["Data"]["Posts"]):
                    raise CollectionError(
                        "adapter_program_invalid", "腾讯代码输出遗漏了原列表岗位，不能声称完整覆盖"
                    )
                cached_lists[listing.key] = parsed
            jobs = {doc.source_job_id: doc for doc in parsed.records}
            raw_ids = [_post_id(post["PostId"]) for post in payload["Data"]["Posts"]]
            if active["stage"] == "list":
                total = payload["Data"].get("Count")
                total = total if type(total) is int and total >= 0 else None
                if state["total"] is not None and total is not None and state["total"] != total:
                    state["errors"].append("腾讯总数在采集期间变化，未视为完整快照")
                repeated = bool(raw_ids) and set(raw_ids).issubset(state["raw_seen"])
                state["total"] = total
                state["raw_seen"] = list(dict.fromkeys([*state["raw_seen"], *raw_ids]))
                active.update(stage="details", posts=list(jobs), index=0)
                checkpoint.save(state)
                if not raw_ids:
                    complete = total == 0 and state["page_index"] == 1 and not state["errors"]
                    return finish(
                        "complete" if complete else "unknown",
                        "腾讯接口返回经代码核验的空列表",
                        True,
                    )
                if repeated:
                    state["errors"].append("腾讯分页重复已有岗位，停止继续请求")
                    return finish("partial", "腾讯分页未前进，保留已有证据", True)
            if (
                active["posts"] != list(jobs)
                or type(active["index"]) is not int
                or not 0 <= active["index"] <= len(active["posts"])
            ):
                raise CollectionError(
                    "checkpoint_code_provenance", "腾讯页内游标与原列表代码证据不一致"
                )
            while active["index"] < len(active["posts"]):
                key = active["posts"][active["index"]]
                if key in state["seen"]:
                    active["index"] += 1
                    checkpoint.save(state)
                    continue
                if new_records >= detail_limit:
                    return finish("partial", "达到本次腾讯详情预算，已保存待处理PostId")
                detail_url = (
                    ORIGIN
                    + DETAIL
                    + "?"
                    + urlencode({"postId": _post_id(key), "language": "zh-cn"})
                )
                detail = fetch(detail_url, "detail")
                if detail is None:
                    return finish("partial", "达到本次腾讯详情读取预算，已保存待处理PostId")
                detail_payload = _payload(detail, post_id=key)
                document, _ = invoke(
                    detail, detail_payload, "parse_detail", jobs[key], listing.requested_url
                )
                document.raw_ref = detail.final_url
                state["seen"].append(key)
                active["index"] += 1
                new_records += 1
                checkpoint.save(state, page=detail, records=[document])
                if progress:
                    progress(f"腾讯代码已核验并保存 {len(state['seen'])} 个岗位详情")
            state["active"] = None
            state["page_index"] += 1
            checkpoint.save(state)
            if state["total"] is not None and len(state["raw_seen"]) >= state["total"]:
                if state["total"] != len(state["raw_seen"]):
                    state["errors"].append("腾讯总数与实际岗位ID数量不一致")
                return finish(
                    "partial" if state["errors"] else "complete",
                    "已遍历腾讯API范围" if not state["errors"] else "腾讯计数不稳定，保留部分覆盖",
                    True,
                )
    except CollectionError as exc:
        exc.code_execution, exc.adapter_fingerprint = proof(True), descriptor.fingerprint
        exc.repair_evidence = (
            capture_program_failure(last_context) if last_context is not None else None
        )
        raise
    finally:
        fetcher.close()
