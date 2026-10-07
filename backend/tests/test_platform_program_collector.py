"""Actual collector, first-party validation and PG runtime; browser/Runner are explicit fixtures."""
from copy import deepcopy

import pytest
from sqlalchemy import select

from app.adapter_code_runtime import ProgramRuntime, freeze_for_task
from app.adapter_maintenance import activate_release
from app.adapter_runtime_models import AdapterProgramExecution
from app.collection import CollectionError
from app.db import session_factory
from app.models import TaskRun
from app.platform_collectors import collect_platform
import test_platform_collectors as platform_support
import test_adapter_code_runtime as runtime_support
import test_adapter_program as program_support

fake_browser = platform_support.fake_browser


class PlatformRunner(runtime_support.RuntimeRunner):
    def get_run(self, run_id):
        request = next(item["request"]["input"] for item in self.runs.values() if item["run_id"] == run_id)
        common = {key: request[key] for key in ("protocol", "input_id", "operation")}
        if request["operation"] == "parse_list":
            self.output = {**common, "records": [{"row": {"document_id": "json", "pointer": "/zpData/jobList/0"},
                "fields": {"id": program_support.claim("job1", "/encryptJobId"),
                           "title": program_support.claim("工程师", "/jobName"),
                           "company": program_support.claim("甲公司", "/brandName"),
                           "company_id": program_support.claim("acme", "/encryptBrandId")}}]}
        elif request["operation"] == "parse_detail":
            self.output = {**common, "row": {"document_id": "dom", "selector": ".job-sec-text"},
                           "fields": {"description": program_support.claim("负责平台研发", selector="")}}
        else:
            self.output = {**common, "navigation": []}
        return super().get_run(run_id)


def prepare(db, monkeypatch, browser):
    state = runtime_support.seed_runtime(db)
    browser["payload"]["zpData"]["jobList"][0]["encryptBrandId"] = "acme"
    browser["detail_html"] = '<main><div class="job-sec-text">负责平台研发</div></main>'
    state.runner = PlatformRunner(state.descriptor.program, {})
    monkeypatch.setattr("app.adapter_code_runtime.ProgramRuntime",
        lambda *args, **kwargs: ProgramRuntime(*args, client=state.runner, wait_seconds=1, **kwargs))
    def reject_native(*args, **kwargs):
        raise AssertionError("代码版本不应调用原生解析器补结果")
    monkeypatch.setattr("app.platform_collectors.extract_jobs", reject_native)
    return state


def collect(state):
    return collect_platform("test-account", "boss", state.snapshot, program_support.IDENTITY,
        {"max_pages": 1, "max_details": 1}, descriptor=state.descriptor,
        runtime_scope={"task_id": state.task.id, "source_run_id": state.source.id, "expected_attempt": 1})


def test_published_code_drives_list_and_detail_through_real_runtime(db, monkeypatch, fake_browser):
    state = prepare(db, monkeypatch, fake_browser)
    result = collect(state)
    assert result.records[0].title == "工程师" and result.records[0].description == "负责平台研发"
    assert result.records[0].observed_at is not None
    assert result.coverage == "partial" and result.adapter_fingerprint == state.descriptor.fingerprint
    assert result.code_execution["verified"] and len(result.code_execution["run_ids"]) == 2
    executions = db.scalars(select(AdapterProgramExecution)).all()
    assert {item.operation for item in executions} == {"parse_list", "parse_detail"}
    assert all(item.status == "accepted" and item.bundle_sha == state.descriptor.program["bundle_sha256"] for item in executions)
    assert len(state.runner.posts) == 2 and fake_browser["saved"]


def test_new_active_during_eventgreenlet_keeps_frozen_program(db, monkeypatch, fake_browser):
    state = prepare(db, monkeypatch, fake_browser)
    def switch():
        activate_release(db, state.old_json, rollback=True, expected_active_release_id=state.release.id,
                         expected_generation=state.registry.release_generation)
        db.commit()
    fake_browser["before_response"] = switch
    result = collect(state)
    assert result.code_execution["fingerprint"] == state.descriptor.fingerprint
    assert all(item["bundle_sha256"] == state.descriptor.program["bundle_sha256"] for item in state.runner.posts)


def test_signed_candidate_failure_does_not_fall_back_and_retains_program_input(db, monkeypatch, fake_browser):
    state = prepare(db, monkeypatch, fake_browser)
    state.runner.status, state.runner.error_code = "failed", "candidate_exit_nonzero"
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "runner_execution_failed"
    assert error.value.code_failure_evidence["type"] == "program_input"
    assert error.value.adapter_fingerprint == state.descriptor.fingerprint
    assert not error.value.code_execution["verified"] and len(fake_browser["navigations"]) == 1
    assert not fake_browser["saved"]


def test_cancelled_task_does_not_start_browser_navigation(db, monkeypatch, fake_browser):
    state = prepare(db, monkeypatch, fake_browser)
    with session_factory()() as changed:
        changed.get(TaskRun, state.task.id).cancel_requested = True
        changed.commit()
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "cancelled"
    assert not state.runner.posts and not fake_browser.get("navigations")


def test_code_version_without_real_source_scope_is_rejected_before_browser(db, monkeypatch, fake_browser):
    state = prepare(db, monkeypatch, fake_browser)
    with pytest.raises(CollectionError) as error:
        collect_platform("test-account", "boss", deepcopy(state.snapshot), program_support.IDENTITY,
                         {"max_pages": 1, "max_details": 1}, descriptor=state.descriptor)
    assert error.value.code == "code_runtime_scope" and not fake_browser.get("navigations")


def test_program_detail_redirect_to_another_job_is_rejected_before_parsing(db, monkeypatch, fake_browser):
    state = prepare(db, monkeypatch, fake_browser)
    fake_browser["detail_final_url"] = "https://www.zhipin.com/job_detail/another-job.html"
    with pytest.raises(CollectionError) as error:
        collect(state)
    assert error.value.code == "identity_review"
    assert [request["operation"] for request in state.runner.posts] == ["parse_list"]
    assert not error.value.code_execution["verified"] and error.value.code_execution["error_code"] == "identity_review"
    assert not fake_browser["saved"]


@pytest.mark.parametrize("field,value", [
    ("expected_adapter_fingerprint", "f" * 64),
    ("expected_release_id", "00000000-0000-0000-0000-000000000001"),
    ("expected_release_generation", 999),
])
def test_recovery_freeze_refuses_changed_publication_before_source_read(db, field, value):
    state = runtime_support.seed_runtime(db)
    db.refresh(state.task)
    state.task.config_snapshot = {**state.task.config_snapshot,
        "sources_snapshot": [{**state.snapshot, field: value}]}
    db.commit()
    with pytest.raises(CollectionError) as error:
        freeze_for_task("boss", task_id=state.task.id, binding_id=state.binding.id, expected_attempt=1)
    assert error.value.code == "adapter_version_conflict"
    assert not state.runner.posts
