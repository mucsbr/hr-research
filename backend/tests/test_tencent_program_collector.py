"""Real checkpoint state with Mock HTTP/Runner; never import or execute a candidate."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
import pytest
from sqlalchemy import select

from app import collection
from app.adapter_code_models import AdapterCodePackage
from app.adapter_code_package import content_sha256
from app.adapter_program import validate_detail_output, validate_list_output
from app.adapter_runtime_models import AdapterProgramExecution
from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
from app.collection import CollectionError
from app.db import session_factory
from app.models import Company, SourceBinding, SourceRun, Subject, TaskRun
from app.official_checkpoint import OfficialCheckpointStore
from app.serialization import row
from app import tencent_program_collector as module


FILTERS = {
    "cities": [],
    "keywords": [],
    "exclusions": [],
    "sources": ["careers"],
    "max_pages": 1,
    "max_details": 1,
}


def response_output(context):
    """An explicit Mock Runner response, validated by the actual first-party boundary."""
    request = context.request
    data = next(item["content"] for item in request["documents"] if item["id"] == "json")
    result = {
        "protocol": "adapter-program/1",
        "input_id": request["input_id"],
        "operation": request["operation"],
        "navigation": [],
    }

    def claim(value, key):
        return {
            "value": re.sub(r"\s+", " ", str(value)).strip(),
            "evidence": {"pointer": "/" + key},
        }

    if request["operation"] == "parse_list":
        records = []
        for index, post in enumerate(data["Data"]["Posts"]):
            fields = {
                name: claim(post[key], key)
                for name, key in {
                    "id": "PostId",
                    "title": "RecruitPostName",
                    "company": "ComName",
                    "location": "LocationName",
                }.items()
                if post.get(key) is not None
            }
            records.append(
                {
                    "row": {"document_id": "json", "pointer": f"/Data/Posts/{index}"},
                    "fields": fields,
                    "job_link_id": post["_program_job_link_id"],
                }
            )
        result["records"] = records
    else:
        mapping = {
            "description": "Responsibility",
            "requirements": "Requirement",
            "title": "RecruitPostName",
            "company": "ComName",
            "location": "LocationName",
            "department": "BGName",
            "function": "CategoryName",
            "experience": "RequireWorkYearsName",
            "source_updated_at": "LastUpdateTime",
        }
        result.update(
            row={"document_id": "json", "pointer": "/Data"},
            fields={
                name: claim(data["Data"][key], key)
                for name, key in mapping.items()
                if data["Data"].get(key) is not None
            },
        )
    return result


@pytest.fixture
def rig(db, monkeypatch):
    state = {
        "now": datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc),
        "requests": [],
        "calls": [],
        "verified": [],
        "pages": {1: [1, 2], 2: [3]},
        "total": 3,
        "detail_code": {},
        "wrong_id": {},
        "callback": None,
        "output_mutation": None,
    }
    monkeypatch.setattr("app.official_checkpoint.now", lambda: state["now"])
    monkeypatch.setattr(collection, "validate_fetch_url", lambda url: "203.0.113.10")
    monkeypatch.setattr(
        collection,
        "collect_tencent",
        lambda *_a, **_k: pytest.fail("The code branch must not call the native collector"),
    )
    descriptor = SimpleNamespace(
        kind="official",
        fingerprint="a" * 64,
        release_id=str(uuid.uuid4()),
        generation=1,
        program={
            "contract": "python_adapter_v1",
            "bundle_sha256": "b" * 64,
            "lock_sha256": "c" * 64,
            "runtime_sha256": "sha256:" + "d" * 64,
            "dependency_environment_sha256": "e" * 64,
        },
    )
    db.add(
        AdapterCodePackage(
            bundle_sha=descriptor.program["bundle_sha256"],
            kind="official",
            file_ref="mock-only-no-source-file",
            lock_sha=descriptor.program["lock_sha256"],
            files_manifest={},
            dependencies=[],
            provenance={"mock": True},
        )
    )
    subject = Subject(name="腾讯")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name="腾讯")
    db.add(company)
    db.flush()
    binding = SourceBinding(
        company_id=company.id,
        kind="careers",
        adapter="tencent",
        entry_url="https://careers.tencent.com/search.html",
    )
    db.add(binding)
    db.flush()
    filters = deepcopy(FILTERS)
    task = TaskRun(
        company_id=company.id,
        status="running",
        attempt=1,
        started_at=state["now"],
        config_snapshot={**filters, "company_identity": {"name": "腾讯", "aliases": []}},
    )
    db.add(task)
    db.flush()
    source = SourceRun(
        task_id=task.id, binding_id=binding.id, status="running", scope_hash="mock-program"
    )
    db.add(source)
    db.commit()
    snapshot = row(binding)
    checkpoint = OfficialCheckpointStore.open(task.id, snapshot, filters, source.id, 1)

    def handler(request):
        parsed = urlsplit(str(request.url))
        query = parse_qs(parsed.query)
        state["requests"].append((parsed.path, dict(query)))
        assert request.method == "GET" and request.headers["host"] == "careers.tencent.com"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if state["callback"]:
            state["callback"]()
        if parsed.path == module.QUERY:
            index = int(query["pageIndex"][0])
            posts = [
                {
                    "PostId": str(value),
                    "RecruitPostName": "工程师" + str(value),
                    "ComName": "腾讯科技子公司",
                    "LocationName": "北京",
                }
                for value in state["pages"].get(index, [])
            ]
            total = (
                state["total"].get(index) if isinstance(state["total"], dict) else state["total"]
            )
            return httpx.Response(200, json={"Code": 200, "Data": {"Count": total, "Posts": posts}})
        assert parsed.path == module.DETAIL
        identifier = query["postId"][0]
        code = state["detail_code"].get(identifier, 200)
        return httpx.Response(
            200,
            json={
                "Code": code,
                "Data": {
                    "PostId": state["wrong_id"].get(identifier, identifier),
                    "RecruitPostName": "工程师" + identifier,
                    "Responsibility": "<p>研发" + identifier + "</p><p>维护系统</p>",
                    "Requirement": "<b>经验要求</b>",
                    "LocationName": "上海",
                    "ComName": "腾讯科技子公司",
                    "BGName": "业务群",
                    "CategoryName": "技术",
                    "RequireWorkYearsName": "三年以上",
                    "LastUpdateTime": "2026-10-05",
                },
            },
        )

    original_fetcher = module.TencentProgramFetcher
    monkeypatch.setattr(
        module,
        "TencentProgramFetcher",
        lambda: original_fetcher(transport=httpx.MockTransport(handler)),
    )

    class Client:
        def verify_result(self, result, *, request, lock_sha256, expected_runtime):
            state["verified"].append(result["run_id"])
            receipt = result["receipt"]
            if (
                receipt["request_sha256"] != content_sha256(request)
                or receipt["output_sha256"] != content_sha256(result["output"])
                or receipt["lock_sha256"] != lock_sha256
                or receipt["runtime_sha256"] != expected_runtime
            ):
                raise CollectionError("runner_receipt_mismatch", "Mock receipt mismatch")
            return result["output"]

    class Runtime:
        def __init__(self, chosen, **kwargs):
            self.descriptor, self.reference, self.args = chosen, chosen.program, kwargs
            self.run_ids, self.errors, self.client = set(), [], Client()

        def evidence(self):
            return {
                "release_id": self.descriptor.release_id,
                "release_generation": 1,
                "fingerprint": self.descriptor.fingerprint,
                "bundle_sha256": self.reference["bundle_sha256"],
                "lock_sha256": self.reference["lock_sha256"],
                "runtime_sha256": self.reference["runtime_sha256"],
                "run_ids": sorted(self.run_ids),
                "verified": bool(self.run_ids) and not self.errors,
            }

        def invoke(self, context):
            state["calls"].append(deepcopy(context.request))
            output = response_output(context)
            if state["output_mutation"]:
                state["output_mutation"](context, output)
            try:
                parsed = (
                    validate_list_output(context, output)
                    if context.request["operation"] == "parse_list"
                    else validate_detail_output(context, output)
                )
            except CollectionError as exc:
                self.errors.append(exc.code)
                raise
            with session_factory()() as session:
                self.args["scope_guard"](session)
                request = {
                    "idempotency_key": content_sha256(
                        {"task": str(self.args["task_id"]), "input": context.request}
                    ),
                    "bundle_sha256": self.reference["bundle_sha256"],
                    "operation": context.request["operation"],
                    "input": context.request,
                }
                key = content_sha256(request)
                item = session.scalar(
                    select(AdapterProgramExecution).where(
                        AdapterProgramExecution.execution_key == key
                    )
                )
                if item is None:
                    remote_id = str(uuid.uuid4())
                    result = {
                        "run_id": remote_id,
                        "status": "succeeded",
                        "output": output,
                        "receipt": {
                            "request_sha256": content_sha256(request),
                            "output_sha256": content_sha256(output),
                            "lock_sha256": self.reference["lock_sha256"],
                            "runtime_sha256": self.reference["runtime_sha256"],
                            "dependency_environment_sha256": self.reference[
                                "dependency_environment_sha256"
                            ],
                        },
                    }
                    item = AdapterProgramExecution(
                        task_id=self.args["task_id"],
                        source_run_id=self.args["source_run_id"],
                        binding_id=binding.id,
                        owner_attempt=self.args["expected_attempt"],
                        execution_key=key,
                        adapter_fingerprint=self.descriptor.fingerprint,
                        bundle_sha=self.reference["bundle_sha256"],
                        operation=context.request["operation"],
                        input_sha=content_sha256(context.request),
                        request_sha=key,
                        request=request,
                        remote_run_id=remote_id,
                        status="accepted",
                        result=result,
                    )
                    session.add(item)
                session.commit()
                self.run_ids.add(item.remote_run_id)
            return parsed

    monkeypatch.setattr(module, "ProgramRuntime", Runtime)
    state.update(
        descriptor=descriptor,
        task=task,
        source=source,
        binding=binding,
        snapshot=snapshot,
        filters=filters,
        checkpoint=checkpoint,
    )
    return state


def collect(state):
    return module.collect_tencent_program(
        state["snapshot"],
        {"name": "腾讯", "aliases": []},
        state["filters"],
        state["task"].id,
        state["source"].id,
        state["checkpoint"].expected_attempt,
        state["descriptor"],
        state["checkpoint"],
    )


def resume(db, state):
    db.refresh(state["task"])
    state["task"].attempt += 1
    state["task"].status = "running"
    state["task"].started_at = state["now"]
    db.commit()
    state["checkpoint"] = OfficialCheckpointStore.open(
        state["task"].id,
        state["snapshot"],
        state["filters"],
        state["source"].id,
        state["task"].attempt,
    )


def test_code_pages_details_checkpoint_resume_and_original_observation_times(db, rig):
    first = collect(rig)
    assert first.coverage == "partial" and len(first.records) == 1
    assert (
        first.records[0].description == "研发1 维护系统"
        and first.records[0].requirements == "经验要求"
    )
    assert first.records[0].department == "业务群" and first.records[0].locations == ["上海"]
    assert (
        first.records[0].recruiting_entity == "腾讯科技子公司"
    )  # Registered group board, not company-name forgery.
    observed = first.records[0].observed_at
    rig["now"] += timedelta(seconds=10)
    resume(db, rig)
    second = collect(rig)
    assert second.coverage == "partial" and len(second.records) == 2
    rig["now"] += timedelta(seconds=10)
    resume(db, rig)
    third = collect(rig)
    assert third.coverage == "complete" and len(third.records) == 3
    assert third.records[0].observed_at == observed and third.records[-1].observed_at == rig["now"]
    assert (
        third.code_execution["verified"]
        and third.adapter_fingerprint == rig["descriptor"].fingerprint
    )
    assert len(rig["requests"]) == 5 and len(rig["calls"]) == 5
    assert Counter(item["operation"] for item in rig["calls"]) == {
        "parse_list": 2,
        "parse_detail": 3,
    }
    assert all(item["board"] == "tencent" for item in rig["calls"])
    assert third.checkpoint_info["inherited_run_ids"] and rig["verified"]
    assert (
        third.checkpoint_info["new_list_requests"] == 0
        and third.checkpoint_info["new_detail_requests"] == 1
    )


def test_finished_checkpoint_rechecks_proof_without_http_or_runner_execution(db, rig):
    rig["pages"] = {1: [1]}
    rig["total"] = 1
    original = collect(rig)
    calls = (len(rig["requests"]), len(rig["calls"]))
    resume(db, rig)
    again = collect(rig)
    assert calls == (len(rig["requests"]), len(rig["calls"]))
    assert (
        again.records[0].observed_at == original.records[0].observed_at
        and again.code_execution["verified"]
    )


@pytest.mark.parametrize("change", ["fingerprint", "native_cursor"])
def test_changed_program_or_native_checkpoint_is_never_relabelled_as_code(db, rig, change):
    collect(rig)
    requests = len(rig["requests"])
    if change == "fingerprint":
        rig["descriptor"].fingerprint = "f" * 64
    else:
        row = db.get(OfficialCheckpoint, rig["checkpoint"].id)
        row.state = {**row.state, "cursor": {**row.state["cursor"], "collector": "tencent"}}
        db.commit()
    with pytest.raises(CollectionError) as error:
        collect(rig)
    assert error.value.code in {"checkpoint_code_changed", "checkpoint_strategy_changed"}
    assert len(rig["requests"]) == requests


@pytest.mark.parametrize("code", [401, 403, 429, 500, "200"])
def test_bad_detail_code_never_runs_program_or_becomes_cached_success(db, rig, code):
    rig["pages"] = {1: [1]}
    rig["total"] = 1
    rig["detail_code"]["1"] = code
    with pytest.raises(CollectionError) as failure:
        collect(rig)
    assert failure.value.code in {"access_restricted", "source_contract_changed"}
    assert (
        failure.value.code_execution["verified"] is False and failure.value.repair_evidence is None
    )
    assert len(rig["calls"]) == 1
    assert len(db.scalars(select(OfficialCheckpointStep)).all()) == 1
    rig["detail_code"].clear()
    rig["now"] += timedelta(seconds=3)
    resume(db, rig)
    result = collect(rig)
    assert result.coverage == "complete"
    assert [path for path, _ in rig["requests"]].count(module.QUERY) == 1
    assert [path for path, _ in rig["requests"]].count(module.DETAIL) == 2


def test_detail_response_cannot_switch_postid(db, rig):
    rig["wrong_id"]["1"] = "999"
    with pytest.raises(CollectionError) as error:
        collect(rig)
    assert error.value.code == "identity_review" and len(rig["calls"]) == 1
    assert not rig["checkpoint"].records()


def test_candidate_cannot_fabricate_description_and_claim_verified(db, rig):
    def mutate(context, output):
        if context.request["operation"] == "parse_detail":
            output["fields"]["description"]["value"] = "没有原文依据的编造职责"

    rig["output_mutation"] = mutate
    with pytest.raises(CollectionError) as error:
        collect(rig)
    assert error.value.code_execution["verified"] is False
    assert error.value.repair_evidence["operation"] == "parse_detail"
    assert not rig["checkpoint"].records()


@pytest.mark.parametrize("change", ["observed_at", "source_updated_at", "receipt", "raw_seen"])
def test_tampered_saved_records_or_receipts_fail_before_new_fetch(db, rig, change):
    collect(rig)
    before = len(rig["requests"])
    detail = next(
        step for step in db.scalars(select(OfficialCheckpointStep)) if step.parsed_records
    )
    if change in {"observed_at", "source_updated_at"}:
        records = deepcopy(detail.parsed_records)
        records[0][change] = "2026-10-06T00:00:00+00:00"
        detail.parsed_records = records
    elif change == "receipt":
        proof = db.scalar(select(AdapterProgramExecution))
        proof.result = {**proof.result, "output": {"forged": True}}
    else:
        cp = db.get(OfficialCheckpoint, rig["checkpoint"].id)
        cp.state = {**cp.state, "cursor": {**cp.state["cursor"], "raw_seen": ["1", "2", "999"]}}
    db.commit()
    with pytest.raises(CollectionError) as error:
        collect(rig)
    assert error.value.code in {"checkpoint_code_provenance", "runner_receipt_mismatch"}
    assert len(rig["requests"]) == before


@pytest.mark.parametrize("action", ["cancel", "replace_attempt"])
def test_cancel_or_attempt_replacement_during_http_does_not_save_evidence(db, rig, action):
    def interrupt():
        with session_factory()() as session:
            task = session.get(TaskRun, rig["task"].id)
            if action == "cancel":
                task.cancel_requested = True
            else:
                task.attempt += 1
            session.commit()

    rig["callback"] = interrupt
    with pytest.raises(CollectionError) as error:
        collect(rig)
    assert error.value.code in {"cancelled", "execution_superseded"}
    assert not db.scalars(select(OfficialCheckpointStep)).all() and not rig["calls"]


@pytest.mark.parametrize(
    "destination",
    [
        "https://evil.invalid/stolen",
        "https://careers.tencent.com/logout",
        "https://careers.tencent.com/tencentcareer/api/post/Query?pageIndex=2&pageSize=1&language=zh-cn&area=cn",
    ],
)
def test_redirect_is_rejected_before_following_any_changed_scope(monkeypatch, destination):
    requested = []
    monkeypatch.setattr(collection, "validate_fetch_url", lambda url: "203.0.113.10")

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={"Location": destination})

    with pytest.raises(CollectionError):
        fetcher = module.TencentProgramFetcher(transport=httpx.MockTransport(handler))
        try:
            fetcher.fetch(
                module.ORIGIN + module.QUERY + "?pageIndex=1&pageSize=1&language=zh-cn&area=cn"
            )
        finally:
            fetcher.close()
    assert len(requested) == 1


def test_explicit_empty_api_count_is_verified_by_program_not_native(db, rig):
    rig["pages"] = {1: []}
    rig["total"] = 0
    result = collect(rig)
    assert (
        result.coverage == "complete" and result.records == [] and result.code_execution["verified"]
    )
    assert len(rig["calls"]) == 1 and rig["calls"][0]["operation"] == "parse_list"


def test_count_changes_never_become_complete(db, rig):
    rig["total"] = {1: 3, 2: 4}
    for _ in range(3):
        result = collect(rig)
        resume(db, rig)
    assert result.coverage in {"partial", "unknown"} and result.errors


def test_explicit_new_task_inherits_only_its_old_code_checkpoint_chain(db, rig):
    first = collect(rig)
    old_task_id = rig["task"].id
    db.refresh(rig["task"])
    rig["task"].status = "partial"
    rig["source"].status = "partial"
    db.commit()
    rig["now"] += timedelta(seconds=15)
    task = TaskRun(
        company_id=rig["task"].company_id,
        status="running",
        attempt=1,
        started_at=rig["now"],
        config_snapshot=deepcopy(rig["task"].config_snapshot),
    )
    db.add(task)
    db.flush()
    source = SourceRun(
        task_id=task.id,
        binding_id=rig["binding"].id,
        status="running",
        scope_hash="explicit-resume",
    )
    db.add(source)
    db.commit()
    checkpoint = OfficialCheckpointStore.open(
        task.id, rig["snapshot"], rig["filters"], source.id, 1, resume_from_task_id=old_task_id
    )
    rig.update(task=task, source=source, checkpoint=checkpoint)
    result = collect(rig)
    assert result.checkpoint_info["resume_from_task_id"] == str(old_task_id)
    assert set(first.code_execution["run_ids"]).issubset(
        result.checkpoint_info["inherited_run_ids"]
    )
    assert result.records[0].observed_at == first.records[0].observed_at
    assert result.records[1].observed_at == rig["now"]
    assert result.code_execution["verified"]


def test_receipt_from_another_source_run_is_not_a_resume_proof(db, rig):
    collect(rig)
    requests = len(rig["requests"])
    stranger = TaskRun(company_id=rig["task"].company_id, status="failed", config_snapshot={})
    db.add(stranger)
    db.flush()
    foreign = SourceRun(
        task_id=stranger.id, binding_id=rig["binding"].id, status="failed", scope_hash="unrelated"
    )
    db.add(foreign)
    db.flush()
    proof = db.scalar(select(AdapterProgramExecution))
    proof.source_run_id = foreign.id
    db.commit()
    with pytest.raises(CollectionError, match="接续链"):
        collect(rig)
    assert len(rig["requests"]) == requests


def test_malformed_binding_port_is_rejected_without_http_or_runner(db, rig):
    rig["snapshot"]["entry_url"] = "https://careers.tencent.com:invalid/search.html"
    with pytest.raises(CollectionError) as error:
        collect(rig)
    assert error.value.code == "code_runtime_scope"
    assert not rig["requests"] and not rig["calls"]
