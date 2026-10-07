import json
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace

import pytest
from playwright._impl._greenlets import EventGreenlet

from app.collection import CollectionError
from app.adapter_maintenance import KINDS, builtin_manifest, use_manifest
from app.platform_collectors import collect_platform, expected_company_id, extract_jobs


@pytest.fixture(autouse=True)
def fixed_contracts():
    # Contract parser tests never read or mutate whichever releases a live app uses.
    with ExitStack() as stack:
        for kind in KINDS:
            stack.enter_context(use_manifest(kind, builtin_manifest(kind)))
        yield


def test_boss_contract_filters_company_and_never_persists_navigation_secrets():
    binding = {"entry_url": "https://www.zhipin.com/gongsi/company123~.html"}
    payload = {"code": 0, "zpData": {"jobList": [
        {"encryptJobId": "job1~", "jobName": "研发工程师", "encryptBrandId": "company123~",
         "brandName": "甲公司", "salaryDesc": "20-30K", "cityName": "上海",
         "jobExperience": "3-5年", "jobDegree": "本科", "securityId": "temporary-secret",
         "lid": "tracking-value", "bossName": "不应保存的招聘者"},
        {"encryptJobId": "job2", "jobName": "另一家公司岗位", "encryptBrandId": "other",
         "brandName": "乙公司", "salaryDesc": "10-20K"},
    ]}}
    jobs, recognized, count = extract_jobs(payload, "boss", binding, {"name": "甲公司"})
    assert recognized and count == 2 and len(jobs) == 1
    doc, navigation = jobs[0]
    assert doc.title == "研发工程师" and doc.salary_raw == "20-30K"
    assert "temporary-secret" in navigation
    assert "temporary-secret" not in doc.model_dump_json()
    assert "不应保存的招聘者" not in doc.model_dump_json()
    assert "?" not in doc.url


def test_boss_name_fallback_requires_explicit_alias():
    payload = {"zpData": {"jobList": [{"encryptJobId": "job1", "jobName": "岗位",
                                      "brandName": "甲集团", "salaryDesc": "面议"}]}}
    binding = {"entry_url": "https://www.zhipin.com/web/geek/job"}
    assert not extract_jobs(payload, "boss", binding, {"name": "甲公司"})[0]
    assert extract_jobs(payload, "boss", binding, {"name": "甲公司", "aliases": ["甲集团"]})[0]


def test_liepin_nested_fields_preserve_missing_values():
    binding = {"entry_url": "https://www.liepin.com/company/12345/"}
    payload = {"data": {"jobCardList": [
        {"job": {"jobId": 999, "title": "产品经理", "dq": "北京"},
         "comp": {"compId": 12345, "compName": "甲公司"}},
    ]}}
    jobs, recognized, count = extract_jobs(payload, "liepin", binding, {"name": "甲公司"})
    assert recognized and count == 1
    assert jobs[0][0].source_job_id == "999"
    assert jobs[0][0].salary_raw is None
    assert jobs[0][0].url == "https://www.liepin.com/job/999.shtml"


def test_encrypted_font_salary_is_not_misreported():
    payload = {"zpData": {"jobList": [{"encryptJobId": "job1", "jobName": "岗位",
                                      "brandName": "甲公司", "salaryDesc": "\ue001-\ue002K"}]}}
    jobs, _, _ = extract_jobs(payload, "boss", {"entry_url": "https://www.zhipin.com/web/geek/job"},
                              {"name": "甲公司"})
    assert jobs[0][0].salary_raw is None


def test_company_id_does_not_truncate_tilde():
    assert expected_company_id("boss", {"entry_url": "https://www.zhipin.com/gongsi/a12%7E.html"}) == "a12~"


def test_internal_browser_api_requires_worker_credential(client):
    response = client.post("/api/internal/browser/collect", json={
        "task_id": "00000000-0000-0000-0000-000000000001",
        "binding_id": "00000000-0000-0000-0000-000000000002", "filters": {}, "expected_attempt": 1,
    })
    assert response.status_code == 403


@pytest.fixture
def fake_browser(monkeypatch):
    """Exercise the collector state machine without cookies, network or model calls."""
    options = {
        "listing_status": 200, "listing_text": "甲公司招聘岗位", "detail_status": 200,
        "detail_text": "岗位职责 负责平台研发", "description": "负责平台研发",
        "payload": {"zpData": {"jobList": [{
            "encryptJobId": "job1", "jobName": "工程师", "brandName": "甲公司",
        }]}}, "response_host": "www.zhipin.com", "saved": False,
    }

    class Locator:
        first = property(lambda self: self)

        def __init__(self, value):
            self.value = value

        def count(self):
            return int(bool(self.value))

        def inner_text(self, **kwargs):
            return self.value

    class Page:
        def __init__(self, detail=False):
            self.detail = detail
            self.url = "about:blank"
            self.listener = None
            self.mouse = SimpleNamespace(wheel=lambda *args: None)

        def set_default_timeout(self, *args):
            pass

        def on(self, _, callback):
            self.listener = callback

        def goto(self, *args, **kwargs):
            self.url = options.get("detail_final_url", args[0]) if self.detail else args[0]
            options.setdefault("navigations", []).append(self.url)
            if self.listener:
                if options.get("before_response"):
                    options["before_response"]()
                response = SimpleNamespace(
                    url=f"https://{options['response_host']}/api/joblist",
                    status=200, headers={"content-type": "application/json"},
                    body=lambda: json.dumps(options["payload"]).encode(),
                )
                # The sync Playwright dispatcher creates a fresh EventGreenlet, whose
                # ContextVars do not inherit the collection's manifest/capture state.
                EventGreenlet(lambda: self.listener(response)).switch()
            return SimpleNamespace(status=options["detail_status" if self.detail else "listing_status"])

        def wait_for_load_state(self, *args, **kwargs):
            pass

        def wait_for_timeout(self, *args):
            pass

        def locator(self, selector):
            if selector == "body":
                return Locator(options["detail_text" if self.detail else "listing_text"])
            if self.detail and "selector_descriptions" in options:
                return Locator(options["selector_descriptions"].get(selector, ""))
            return Locator(options["description"] if self.detail else "")

        def content(self):
            return options.get("detail_html" if self.detail else "listing_html", "")

    class Context:
        count = 0

        def new_page(self):
            page = Page(detail=bool(self.count))
            self.count += 1
            return page

        def route(self, *args):
            pass

        def storage_state(self, **kwargs):
            return {"cookies": [], "origins": []}

        def close(self):
            pass

    class Playwright:
        def __enter__(self):
            self.chromium = SimpleNamespace(launch=lambda **kwargs: SimpleNamespace(
                new_context=lambda **kwargs: Context(), close=lambda: None))
            return self

        def __exit__(self, *args):
            pass

    def save(*args):
        options["saved"] = True

    monkeypatch.setattr("app.platform_collectors.sync_playwright", Playwright)
    monkeypatch.setattr("app.platform_collectors.load_state", lambda _: {"cookies": [], "origins": []})
    monkeypatch.setattr("app.platform_collectors.save_state", save)
    return options


def run_fake():
    return collect_platform("test-account", "boss", {"entry_url": "https://www.zhipin.com/web/geek/job"},
                            {"name": "甲公司"}, {"max_pages": 1, "max_details": 1})


@pytest.mark.parametrize(("field", "value", "code"), [
    ("listing_text", "请先登录", "waiting_login"),
    ("listing_text", "完成安全验证", "waiting_verification"),
    ("listing_status", 429, "waiting_verification"),
    ("detail_status", 401, "waiting_login"),
    ("detail_status", 404, "http_error"),
    ("detail_text", "登录查看完整岗位", "waiting_login"),
    ("description", "", "source_contract_changed"),
    ("response_host", "unrelated.example.com", "source_contract_changed"),
])
def test_platform_blocked_or_invalid_reads_never_commit(fake_browser, field, value, code):
    fake_browser[field] = value
    with pytest.raises(CollectionError) as exc:
        run_fake()
    assert exc.value.code == code
    assert fake_browser["saved"] is False


def test_platform_valid_read_stays_partial_and_saves_session(fake_browser):
    result = run_fake()
    assert len(result.records) == 1
    assert result.records[0].description == "负责平台研发"
    assert result.coverage == "partial"
    assert fake_browser["saved"] is True


def test_native_detail_redirect_to_another_job_cannot_reuse_its_body(fake_browser):
    fake_browser["detail_final_url"] = "https://www.zhipin.com/job_detail/other.html"
    with pytest.raises(CollectionError) as error:
        run_fake()
    assert error.value.code == "identity_review"
    assert not fake_browser["saved"]


def publish_manifest_before_response(fake_browser, monkeypatch):
    baseline = builtin_manifest("boss")
    newer = deepcopy(baseline)
    newer["version"] = "published-during-response"
    newer["fields"]["title"] = [["newTitle"]]
    live = {"manifest": baseline}
    reads = []

    def active(_):
        reads.append(live["manifest"]["version"])
        return deepcopy(live["manifest"])

    fake_browser["before_response"] = lambda: live.update(manifest=newer)
    monkeypatch.setattr("app.adapter_maintenance.active_manifest", active)
    return baseline, live, reads


def test_response_event_greenlet_keeps_frozen_manifest_during_publish(fake_browser, monkeypatch):
    from app.adapter_maintenance import fingerprint

    baseline, live, reads = publish_manifest_before_response(fake_browser, monkeypatch)
    fake_browser["payload"]["zpData"]["jobList"][0]["newTitle"] = "不能混用新版本字段"
    result = run_fake()
    assert live["manifest"]["version"] == "published-during-response"
    assert result.records[0].title == "工程师"
    assert result.adapter_fingerprint == fingerprint(baseline)
    assert "published-during-response" not in reads


def test_response_event_greenlet_captures_moved_list_with_frozen_fields(fake_browser, monkeypatch):
    from app.adapter_maintenance import fingerprint

    baseline, live, reads = publish_manifest_before_response(fake_browser, monkeypatch)
    fake_browser["payload"] = {"zpData": {"vacanciesV2": [{
        "encryptJobId": "job1", "jobName": "工程师", "brandName": "甲公司",
        "securityId": "private-navigation", "bossName": "个人姓名", "phone": "13812345678",
    }]}}
    with pytest.raises(CollectionError) as exc:
        run_fake()
    assert exc.value.code == "source_contract_changed"
    assert live["manifest"]["version"] == "published-during-response"
    assert exc.value.adapter_fingerprint == fingerprint(baseline)
    evidence = exc.value.repair_evidence
    assert evidence and evidence["type"] == "response_list"
    assert evidence["expected"] == [{"source_job_id": "job1", "title": "工程师", "recruiting_entity": "甲公司"}]
    assert evidence["payload"]["zpData"]["vacanciesV2"][0]["jobName"] == "工程师"
    assert not any(secret in json.dumps(evidence, ensure_ascii=False)
                   for secret in ["private-navigation", "个人姓名", "13812345678"])
    assert "published-during-response" not in reads
    assert fake_browser["saved"] is False


def test_platform_no_saved_state_requests_login_without_opening_browser(monkeypatch):
    monkeypatch.setattr("app.platform_collectors.load_state", lambda _: None)
    monkeypatch.setattr("app.platform_collectors.sync_playwright", lambda: pytest.fail("must not open a browser"))
    with pytest.raises(CollectionError) as exc:
        run_fake()
    assert exc.value.code == "waiting_login"


def test_zhaopin_saved_account_uses_browser_document_contract(fake_browser):
    from test_extended_platforms import BINDING, IDENTITY, zhao_detail, zhao_listing

    fake_browser.update(listing_html=zhao_listing(), detail_html=zhao_detail(),
                        payload={}, response_host="www.zhaopin.com")
    result = collect_platform("saved-account", "zhaopin", BINDING, IDENTITY,
                              {"max_pages": 1, "max_details": 1})
    assert len(result.records) == 1
    assert result.records[0].description == "负责系统研发"
    assert result.coverage == "partial" and fake_browser["saved"] is True


def test_new_platform_anonymous_dynamic_fallback_does_not_save_account(fake_browser, monkeypatch):
    def dynamic(*args, **kwargs):
        raise CollectionError("adaptation_required", "browser required")

    monkeypatch.setattr("app.platform_documents.collect_public_platform", dynamic)
    from test_extended_platforms import BINDING, IDENTITY, zhao_detail, zhao_listing

    fake_browser.update(listing_html=zhao_listing(), detail_html=zhao_detail(),
                        payload={}, response_host="www.zhaopin.com")
    result = collect_platform(None, "zhaopin", BINDING, IDENTITY,
                              {"max_pages": 1, "max_details": 1})
    assert len(result.records) == 1
    assert fake_browser["saved"] is False
