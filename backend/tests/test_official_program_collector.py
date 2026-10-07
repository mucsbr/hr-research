"""HTTP/model/Runner fixtures with real PG, Runtime, checkpoint and first-party validation."""
from copy import deepcopy
from datetime import timedelta
import json

from bs4 import BeautifulSoup
import httpx
import pytest
from sqlalchemy import func, select

from app.adapter_code_runtime import ProgramRuntime, freeze_for_task
from app.adapter_runtime_models import AdapterProgramExecution
from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
from app.collection import CollectionError, CollectionResult, jsonld_jobs
from app.db import session_factory
from app.llm import ModelGateway
from app.models import JobSourceRecord, ModelCall, SourceRun, TaskRun
from app.ingest import ingest
from app.official_checkpoint import OfficialCheckpointStore, plan_digest
from app.official_program_collector import OriginPublicFetcher, capture_native_official_failure, collect_official_program
from app.website_agent import collect_official
import app.official_program_collector as module
import test_adapter_code_runtime as runtime_support
import test_adapter_program as program_support

ENTRY = "https://careers.example/jobs"
DESCRIPTION = "岗位职责：负责企业平台研发、数据处理与接口设计，持续优化服务可靠性和用户体验。"
PLAN = {"render_mode": "http", "job_link_selector": "a.job", "title_selector": "h1",
        "description_selector": ".description", "requirements_selector": None, "location_selector": None,
        "salary_selector": None, "next_selector": "a.next", "complete_selector": None, "total_selector": None}


def posting(number, *, identifier=True):
    value = {"@context": "https://schema.org", "@type": "JobPosting", "title": "研发工程师" + str(number),
             "url": ENTRY + "/" + str(number), "description": DESCRIPTION,
             "hiringOrganization": {"name": "甲公司"}, "jobLocation": {"address": {"addressLocality": "上海"}}}
    if identifier:
        value["identifier"] = {"value": "native-id-" + str(number)}
    return value


def structured(*jobs):
    return "".join('<script type="application/ld+json">' + json.dumps(job, ensure_ascii=False) + '</script>' for job in jobs)


def oracle(request):
    """Independent fixture response builder; never imports adapters/programs or candidate source."""
    result = {"protocol": request["protocol"], "input_id": request["input_id"], "operation": request["operation"]}
    docs = {item["id"]: item["content"] for item in request["documents"]}
    blocks = (docs.get("json") or {}).get("blocks", [])
    if request["operation"] == "navigation":
        next_link = next((link["id"] for link in request["links"] if link["purpose"] == "next"), None)
        return {**result, "navigation": [{"type": "follow", "link_id": next_link}] if next_link else []}
    if request["operation"] == "parse_list":
        records = []
        for index, job in enumerate(blocks):
            if isinstance(job, dict) and job.get("_program_job_link_id"):
                records.append({"row": {"document_id": "json", "pointer": "/blocks/" + str(index)},
                    "fields": {"title": program_support.claim(job["title"], "/title"),
                               "company": program_support.claim(job["hiringOrganization"]["name"], "/hiringOrganization/name")},
                    "job_link_id": job["_program_job_link_id"]})
        if not records and request.get("plan"):
            for node in BeautifulSoup(docs["dom"], "html.parser").select("a.job[data-program-link-id]"):
                records.append({"row": {"document_id": "dom", "selector": "#" + node["id"]},
                    "fields": {"title": program_support.claim(node.get_text(" ", strip=True), selector="")},
                    "job_link_id": node["data-program-link-id"]})
        return {**result, "records": records}
    for index, job in enumerate(blocks):
        if isinstance(job, dict) and job.get("description"):
            return {**result, "row": {"document_id": "json", "pointer": "/blocks/" + str(index)},
                    "fields": {"description": program_support.claim(job["description"], "/description")}}
    node = BeautifulSoup(docs["dom"], "html.parser").select_one(".description")
    return {**result, "row": {"document_id": "dom", "selector": ".description"},
            "fields": {"description": program_support.claim(node.get_text(" ", strip=True), selector="")}}


class WebsiteRunner(runtime_support.RuntimeRunner):
    def get_run(self, run_id):
        stored = next(value for value in self.runs.values() if value["run_id"] == run_id)
        self.output = oracle(stored["request"]["input"])
        if getattr(self, "transform", None):
            self.output = self.transform(stored["request"]["input"], self.output)
        return super().get_run(run_id)


class Pages:
    def __init__(self, routes):
        self.routes, self.calls, self.closed = routes, [], []
        self.callback = None

    def factory(self, mode):
        parent = self
        class Fetcher:
            check = None
            navigation_guard = None
            def fetch(self, url):
                if self.check:
                    self.check()
                if self.navigation_guard:
                    self.navigation_guard(url)
                parent.calls.append((mode, url))
                if parent.callback:
                    parent.callback(url)
                value = parent.routes[(mode, url)] if (mode, url) in parent.routes else parent.routes[url]
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, tuple) else (url, value)
            def close(self):
                parent.closed.append(mode)
        return Fetcher()


def prepare(db, monkeypatch, routes, *, filters=None, plan=None):
    state = runtime_support.seed_runtime(db, kind="official")
    filters = filters or {"max_pages": 2, "max_details": 2}
    db.refresh(state.task)
    if plan is not None:
        state.binding.config = {"website_plan": plan, "plan_hash": plan_digest(plan)}
        db.flush()
        from app.serialization import row
        state.snapshot = row(state.binding)
    state.task.config_snapshot = {**state.task.config_snapshot, **filters, "sources_snapshot": [state.snapshot]}
    db.commit()
    state.filters = filters
    state.checkpoint = OfficialCheckpointStore.open(state.task.id, state.snapshot, filters, state.source.id, 1)
    state.runner = WebsiteRunner(state.descriptor.program, {})
    pages = Pages(routes)
    monkeypatch.setattr(module, "OriginPublicFetcher", lambda entry: pages.factory("http"))
    monkeypatch.setattr(module, "BrowserCapture", lambda: pages.factory("browser"))
    monkeypatch.setattr(module, "ProgramRuntime", lambda *args, **kwargs: ProgramRuntime(*args, client=state.runner, wait_seconds=1, **kwargs))
    return state, pages


def collect(state):
    return collect_official_program(state.snapshot, program_support.IDENTITY, state.filters, state.task.id,
        state.source.id, 1, state.descriptor, state.checkpoint)


def resume(db, state):
    db.refresh(state.task)
    state.task.status = "partial"
    state.source.status = "partial"
    db.commit()
    old_id = state.task.id
    config = {key: value for key, value in state.task.config_snapshot.items() if key != "adapter_versions"}
    task = TaskRun(company_id=state.task.company_id, status="running", attempt=1, started_at=runtime_support.now(),
                   config_snapshot=deepcopy(config))
    db.add(task)
    db.flush()
    source = SourceRun(task_id=task.id, binding_id=state.binding.id, status="running", scope_hash="runtime-test-scope")
    db.add(source)
    db.commit()
    state.task, state.source = task, source
    state.descriptor = freeze_for_task("official", task_id=task.id, binding_id=state.binding.id, expected_attempt=1)
    state.checkpoint = OfficialCheckpointStore.open(task.id, state.snapshot, state.filters, source.id, 1, resume_from_task_id=old_id)


def test_structured_pages_use_real_runtime_keep_native_ids_and_make_no_site_model_call(db, monkeypatch):
    first, second = posting(1), posting(2, identifier=False)
    routes = {ENTRY: structured(first, second), ENTRY + "/1": structured(first), ENTRY + "/2": structured(second)}
    state, pages = prepare(db, monkeypatch, routes)
    before = db.scalar(select(func.count()).select_from(ModelCall))
    result = collect(state)
    assert result.coverage == "unknown" and result.code_execution["verified"]
    assert {item.source_job_id for item in result.records} == {"native-id-1", ENTRY + "/2"}
    assert {item.source_job_id for item in result.records} == {doc.source_job_id for doc in jsonld_jobs(routes[ENTRY], ENTRY)}
    assert all(item.description == DESCRIPTION and item.observed_at is not None for item in result.records)
    assert result.adapter_fingerprint == state.descriptor.fingerprint
    assert len(pages.calls) == 3 and len(state.runner.posts) == 4
    assert db.scalar(select(func.count()).select_from(ModelCall)) == before
    assert db.scalar(select(func.count()).select_from(AdapterProgramExecution).where(AdapterProgramExecution.status == "accepted")) == 4


def test_checkpoint_resume_reads_only_unfinished_detail_and_keeps_observation_time(db, monkeypatch):
    first, second = posting(1), posting(2, identifier=False)
    routes = {ENTRY: structured(first, second), ENTRY + "/1": structured(first), ENTRY + "/2": structured(second)}
    state, pages = prepare(db, monkeypatch, routes, filters={"max_pages": 1, "max_details": 1})
    part = collect(state)
    assert part.coverage == "partial" and len(part.records) == 1
    original_time, original_runs = part.records[0].observed_at, set(part.code_execution["run_ids"])
    resume(db, state)
    result = collect(state)
    assert len(result.records) == 2 and result.code_execution["verified"]
    assert result.records[0].observed_at == original_time
    assert set(result.checkpoint_info["inherited_run_ids"]) == original_runs
    assert pages.calls == [("http", ENTRY), ("http", ENTRY + "/1"), ("http", ENTRY + "/2")]
    assert len(state.runner.posts) == 4


def test_resume_rejects_changed_observation_or_foreign_proof(db, monkeypatch):
    first, second = posting(1), posting(2)
    state, pages = prepare(db, monkeypatch, {ENTRY: structured(first, second), ENTRY + "/1": structured(first),
                                           ENTRY + "/2": structured(second)}, filters={"max_pages": 1, "max_details": 1})
    collect(state)
    resume(db, state)
    step = db.scalar(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == state.checkpoint.id,
                                                         OfficialCheckpointStep.final_url == ENTRY + "/1"))
    records = deepcopy(step.parsed_records)
    records[0]["observed_at"] = (step.captured_at + timedelta(seconds=1)).isoformat()
    step.parsed_records = records
    db.commit()
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "checkpoint_code_provenance" and not error.value.code_execution["verified"]
    assert len(pages.calls) == 2


def test_saved_css_plan_redirect_preserves_native_final_url_identity(db, monkeypatch):
    listing = '<a id="j1" class="job" href="/jobs/1">研发工程师1</a>'
    detail = '<h1>研发工程师1</h1><p class="description">' + DESCRIPTION + '</p>'
    state, pages = prepare(db, monkeypatch, {ENTRY: listing, ENTRY + "/1": (ENTRY + "/final-1", detail)}, plan=PLAN)
    before = db.scalar(select(func.count()).select_from(ModelCall))
    result = collect(state)
    assert result.records[0].source_job_id == ENTRY + "/final-1"
    assert result.records[0].url == ENTRY + "/final-1" and result.code_execution["verified"]
    assert db.scalar(select(func.count()).select_from(ModelCall)) == before and len(pages.calls) == 2


def test_missing_css_plan_is_learned_once_without_native_collection_or_duplicate_reads(db, monkeypatch):
    listing = '<header>private-account person@example.com</header><a id="j1" class="job" href="/jobs/1">研发工程师1</a>'
    detail = '<h1>研发工程师1</h1><p class="description">' + DESCRIPTION + '</p><script>token="PRIVATE_COOKIE"</script>'
    state, pages = prepare(db, monkeypatch, {ENTRY: listing, ENTRY + "/1": detail}, filters={"max_pages": 1, "max_details": 1})
    calls = []
    def response(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert "PRIVATE_COOKIE" not in str(payload) and "person@example.com" not in str(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(PLAN)}, "finish_reason": "stop"}]})
    monkeypatch.setattr("app.website_agent.ModelGateway", lambda: ModelGateway(httpx.MockTransport(response)))
    result = collect(state)
    assert result.records and result.code_execution["verified"] and result.checkpoint_info["site_plan_hash"] == plan_digest(PLAN)
    assert len(calls) == 1 and pages.calls == [("http", ENTRY), ("http", ENTRY + "/1")]
    assert len(state.runner.posts) == 3
    again = collect(state)
    assert again.code_execution["verified"] and len(calls) == 1 and len(pages.calls) == 2


def test_unconfirmed_plan_generation_is_not_charged_again_on_resume(db, monkeypatch):
    state, pages = prepare(db, monkeypatch, {ENTRY: '<a id="j1" class="job" href="/jobs/1">研发工程师</a>',
                                           ENTRY + "/1": '<h1>研发工程师</h1><p class="description">' + DESCRIPTION + '</p>'})
    calls = []
    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("simulated", request=request)
    monkeypatch.setattr("app.website_agent.ModelGateway", lambda: ModelGateway(httpx.MockTransport(fail)))
    with pytest.raises(CollectionError):
        collect(state)
    resume(db, state)
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "checkpoint_adaptation_unconfirmed"
    assert len(calls) == 1 and len(pages.calls) == 2 and state.runner.posts == []


def test_login_failure_and_foreign_redirect_never_become_zero_job_success(db, monkeypatch):
    state, pages = prepare(db, monkeypatch, {ENTRY: '<p>请先登录后查看</p>'})
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "waiting_login" and not error.value.code_execution["verified"]
    assert state.runner.posts == [] and error.value.repair_evidence is None


def test_public_fetcher_rejects_cross_origin_redirect_before_second_request(monkeypatch):
    calls = []
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda url: "93.184.216.34")
    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://foreign.example/jobs"})
    fetcher = OriginPublicFetcher(ENTRY, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(CollectionError) as error:
            fetcher.fetch(ENTRY)
        assert error.value.code == "source_host_mismatch" and len(calls) == 1
    finally:
        fetcher.close()


def test_callback_cancel_after_http_cannot_persist_or_parse_page(db, monkeypatch):
    state, pages = prepare(db, monkeypatch, {ENTRY: structured(posting(1))})
    def cancel(url):
        with session_factory()() as current:
            current.get(TaskRun, state.task.id).cancel_requested = True
            current.commit()
    pages.callback = cancel
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "cancelled" and not error.value.code_execution["verified"]
    assert state.runner.posts == []
    assert db.scalar(select(func.count()).select_from(OfficialCheckpointStep)) == 0


def test_changed_code_fingerprint_cannot_resume_old_cursor(db, monkeypatch):
    state, _ = prepare(db, monkeypatch, {ENTRY: structured(posting(1)), ENTRY + "/1": structured(posting(1))})
    checkpoint = db.get(OfficialCheckpoint, state.checkpoint.id)
    checkpoint.state = {"cursor": {"collector": "official_program", "adapter_fingerprint": "f" * 64}}
    db.commit()
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "checkpoint_code_changed" and not error.value.code_execution["verified"]


@pytest.mark.parametrize("omitted,budget,expected", [(False, 2, "complete"), (True, 2, "partial"), (False, 1, "partial")])
def test_first_party_inventory_and_end_evidence_control_coverage(db, monkeypatch, omitted, budget, expected):
    listing = ('<span class="total">共2个职位</span><a id="j1" class="job" href="/jobs/1">研发工程师1</a>'
               '<a id="j2" class="job" href="/jobs/2">研发工程师2</a><span class="end">没有更多职位</span>')
    plan = {**PLAN, "total_selector": ".total", "complete_selector": ".end"}
    state, _ = prepare(db, monkeypatch, {ENTRY: listing, ENTRY + "/1": '<h1>研发工程师1</h1><p class="description">' + DESCRIPTION + '</p>',
        ENTRY + "/2": '<h1>研发工程师2</h1><p class="description">' + DESCRIPTION + '</p>'},
        plan=plan, filters={"max_pages": 1, "max_details": budget})
    if omitted:
        state.runner.transform = lambda request, result: ({**result, "records": result["records"][:1]}
                                                          if request["operation"] == "parse_list" else result)
    result = collect(state)
    assert result.coverage == expected and result.code_execution["verified"]
    if omitted:
        assert any("未覆盖" in reason for reason in result.errors)


def test_explicit_zero_inventory_can_complete_without_plan_model(db, monkeypatch):
    plan = {**PLAN, "total_selector": ".total", "complete_selector": ".end"}
    state, pages = prepare(db, monkeypatch, {ENTRY: '<span class="total">共0个职位</span><span class="end">没有更多职位</span>'}, plan=plan)
    before = db.scalar(select(func.count()).select_from(ModelCall))
    result = collect(state)
    assert result.records == [] and result.coverage == "complete" and result.code_execution["verified"]
    assert len(pages.calls) == 1 and db.scalar(select(func.count()).select_from(ModelCall)) == before


def test_observed_next_page_omitted_by_program_cannot_be_complete(db, monkeypatch):
    first = posting(1)
    listing = structured(first) + '<a rel="next" href="?page=2">下一页</a><span class="end">没有更多职位</span>'
    state, pages = prepare(db, monkeypatch, {ENTRY: listing, ENTRY + "/1": structured(first)})
    state.binding.config = {"complete_selector": ".end"}
    db.flush()
    from app.serialization import row
    state.snapshot = row(state.binding)
    db.refresh(state.task)
    state.task.config_snapshot = {**state.task.config_snapshot, "sources_snapshot": [state.snapshot]}
    db.commit()
    # Re-open a fresh checkpoint because the administrator changed the source before collection.
    checkpoint = db.get(OfficialCheckpoint, state.checkpoint.id)
    db.delete(checkpoint)
    db.commit()
    state.checkpoint = OfficialCheckpointStore.open(state.task.id, state.snapshot, state.filters, state.source.id, 1)
    state.runner.transform = lambda request, result: ({**result, "navigation": []}
                                                      if request["operation"] == "navigation" else result)
    result = collect(state)
    assert result.coverage == "partial" and any("下一页" in reason for reason in result.errors)
    assert len(pages.calls) == 2


def test_native_css_failure_can_provide_first_code_evidence_without_refetch_or_model(db, monkeypatch):
    listing = '<a id="j1" class="job" href="/jobs/1">研发工程师1</a>'
    detail = '<h1>研发工程师1</h1><p class="description">' + DESCRIPTION + '</p><script>secret="PRIVATE_COOKIE"</script>'
    plan = {**PLAN, "description_selector": ".old-description"}
    state, pages = prepare(db, monkeypatch, {ENTRY: listing, ENTRY + "/1": detail}, plan=plan)
    monkeypatch.setattr("app.collection.PublicFetcher", lambda: pages.factory("http"))
    def forbidden_graph(*args, **kwargs):
        raise AssertionError("已有计划失败应先交给代码维护，不能先花网站规则模型预算")
    monkeypatch.setattr("app.website_agent.website_graph.invoke", forbidden_graph)
    before = db.scalar(select(func.count()).select_from(ModelCall))
    with pytest.raises(CollectionError) as failure:
        collect_official(state.snapshot, state.filters, state.task.id, checkpoint=state.checkpoint, defer_parse_repair=True)
    assert failure.value.code == "selector_mismatch"
    assert db.scalar(select(func.count()).select_from(OfficialCheckpointStep)) == 1
    evidence = capture_native_official_failure(state.snapshot, program_support.IDENTITY, state.checkpoint, failure.value.code)
    assert evidence["type"] == "program_input" and evidence["operation"] == "parse_detail"
    assert "PRIVATE_COOKIE" not in json.dumps(evidence) and "expected" not in evidence
    assert "code_execution" not in evidence and len(pages.calls) == 2 and state.runner.posts == []
    assert db.scalar(select(func.count()).select_from(OfficialCheckpointStep)) == 2
    assert db.scalar(select(func.count()).select_from(ModelCall)) == before
    assert capture_native_official_failure(state.snapshot, program_support.IDENTITY, state.checkpoint, "waiting_login") is None


def test_native_to_code_job_without_identifier_does_not_create_false_new_record(db, monkeypatch):
    job = posting(1, identifier=False)
    state, _ = prepare(db, monkeypatch, {ENTRY: structured(job), ENTRY + "/1": structured(job)})
    native_task = TaskRun(company_id=state.task.company_id, status="succeeded", scope_hash="native-scope")
    db.add(native_task)
    db.flush()
    native_source = SourceRun(task_id=native_task.id, binding_id=state.binding.id, scope_hash="native-scope",
                              status="succeeded", adapter_version="native-0.1")
    db.add(native_source)
    db.flush()
    native = jsonld_jobs(structured(job), ENTRY + "/1")
    ingest(db, native_task, state.binding, native_source, CollectionResult(records=native, coverage="complete"))
    db.commit()
    original = db.scalar(select(JobSourceRecord))
    first_seen = original.first_seen_at
    result = collect(state)
    assert result.code_execution["verified"] and result.records[0].source_job_id == native[0].source_job_id
    ingest(db, state.task, state.binding, state.source, result)
    db.commit()
    db.refresh(original)
    assert db.scalar(select(func.count()).select_from(JobSourceRecord)) == 1
    assert original.first_seen_at == first_seen and state.source.counts["first_seen"] == 0


def test_saved_browser_plan_uses_scoped_browser_without_http_fallback(db, monkeypatch):
    plan = {**PLAN, "render_mode": "browser"}
    state, pages = prepare(db, monkeypatch, {ENTRY: '<a id="j1" class="job" href="/jobs/1">研发工程师</a>',
        ENTRY + "/1": '<h1>研发工程师</h1><p class="description">' + DESCRIPTION + '</p>'}, plan=plan)
    result = collect(state)
    assert result.records and result.code_execution["verified"]
    assert pages.calls == [("browser", ENTRY), ("browser", ENTRY + "/1")]


def test_browser_discovery_cannot_expand_a_one_page_budget_or_call_model(db, monkeypatch):
    state, pages = prepare(db, monkeypatch, {ENTRY: '<main>正在加载职位</main>'},
                           filters={"max_pages": 1, "max_details": 1})
    before = db.scalar(select(func.count()).select_from(ModelCall))
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "official_page_budget" and not error.value.code_execution["verified"]
    assert pages.calls == [("http", ENTRY)] and state.runner.posts == []
    assert db.scalar(select(func.count()).select_from(ModelCall)) == before


@pytest.mark.parametrize("saved,defer", [(True, False), (False, True)])
def test_native_default_repair_and_first_plan_learning_are_preserved(db, monkeypatch, saved, defer):
    plan = {**PLAN, "description_selector": ".old-description"} if saved else None
    state, pages = prepare(db, monkeypatch, {ENTRY: '<a id="j1" class="job" href="/jobs/1">研发工程师</a>',
        ENTRY + "/1": '<h1>研发工程师</h1><p class="description">' + DESCRIPTION + '</p>'}, plan=plan)
    monkeypatch.setattr("app.collection.PublicFetcher", lambda: pages.factory("http"))
    calls = []
    def graph(value):
        calls.append(value)
        return {"result": CollectionResult(coverage="unknown", stop_reason="fixture graph")}
    monkeypatch.setattr("app.website_agent.website_graph.invoke", graph)
    result = collect_official(state.snapshot, state.filters, state.task.id, checkpoint=state.checkpoint,
                              defer_parse_repair=defer)
    assert result.stop_reason == "fixture graph" and len(calls) == 1
