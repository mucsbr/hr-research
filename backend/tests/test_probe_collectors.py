"""Exercise real parsers with a remote-browser contract fixture, never a real website."""

import uuid

import pytest

from app.adapter_maintenance import builtin_manifest, use_manifest
from app.collection import CollectionError
from app.platform_collectors import collect_platform
from app.probe_browser import ProbeSentimentFetcher


class RemoteFixture:
    instances = []
    fail_second_detail = False

    def __init__(self, account_id, frozen, task_id, attempt):
        self.pages = {}
        self.closed = False
        self.instances.append(self)
        assert frozen["mode"] == "probe" and task_id and attempt == 1

    def call(self, operation, payload=None):
        payload = payload or {}
        page_id = payload.get("page_id")
        if operation == "browser.start_task":
            self.pages[page_id] = {}
            return {"created": True}
        if operation == "browser.navigate":
            url = payload["url"]
            if self.fail_second_detail and "job2" in url:
                raise CollectionError("waiting_device", "fixture device offline")
            html = '<html><body><div class="job-sec-text">岗位职责：研发与维护软件系统，参与代码评审。</div></body></html>'
            data = {
                "url": url.split("?", 1)[0],
                "html": html,
                "text": "岗位职责：研发与维护软件系统",
                "status": 200,
            }
            if "/gongsi/" in url:
                data["responses"] = [
                    {
                        "url": "https://www.zhipin.com/wapi/zpgeek/search/joblist.json",
                        "status": 200,
                        "content_type": "application/json",
                        "body": {
                            "code": 0,
                            "zpData": {
                                "jobList": [
                                    {
                                        "encryptJobId": "job1",
                                        "jobName": "测试研发岗位",
                                        "encryptBrandId": "company123~",
                                        "brandName": "测试公司",
                                        "securityId": "fixture-navigation-only",
                                    },
                                    {
                                        "encryptJobId": "job2",
                                        "jobName": "测试产品岗位",
                                        "encryptBrandId": "company123~",
                                        "brandName": "测试公司",
                                    },
                                ]
                            },
                        },
                    }
                ]
            self.pages[page_id] = {**data, "responses": []}
            return data
        if operation == "browser.read_page":
            return self.pages[page_id]
        if operation == "browser.locate":
            return {
                "count": 1,
                "text": "岗位职责：研发与维护软件系统，参与代码评审。",
                "visible": True,
                "disabled": False,
                "attributes": {},
            }
        if operation == "browser.close_session":
            self.closed = True
            return {"closed": True}
        raise AssertionError("unexpected fixture operation")

    def close(self):
        self.closed = True


@pytest.fixture
def remote(monkeypatch):
    RemoteFixture.instances = []
    RemoteFixture.fail_second_detail = False
    monkeypatch.setattr("app.probe_browser.ProbeBrowser", RemoteFixture)

    def forbidden(*args, **kwargs):
        pytest.fail("probe collection cannot launch server Chromium or export cookies")

    monkeypatch.setattr("app.platform_collectors.sync_playwright", forbidden)
    monkeypatch.setattr("app.platform_collectors.load_state", forbidden)
    monkeypatch.setattr("app.platform_collectors.save_state", forbidden)
    return {
        "mode": "probe",
        "device_id": str(uuid.uuid4()),
        "profile_id": str(uuid.uuid4()),
        "revision": str(uuid.uuid4()),
    }


def collect(remote):
    with use_manifest("boss", builtin_manifest("boss")):
        return collect_platform(
            uuid.uuid4(),
            "boss",
            {
                "entry_url": "https://www.zhipin.com/gongsi/company123~.html",
                "browser_session": remote,
            },
            {"name": "测试公司"},
            {"max_pages": 1, "max_details": 2},
            runtime_scope={"task_id": uuid.uuid4(), "expected_attempt": 1},
        )


def test_probe_recruitment_uses_existing_parser_without_server_browser_or_cookie_export(remote):
    result = collect(remote)
    assert len(result.records) == 2 and result.coverage == "partial"
    assert result.records[0].description.startswith("岗位职责")
    assert "fixture-navigation-only" not in result.records[0].model_dump_json()
    assert all(r.closed for r in RemoteFixture.instances)


def test_device_interruption_preserves_already_validated_jobs(remote):
    RemoteFixture.fail_second_detail = True
    result = collect(remote)
    assert len(result.records) == 1 and result.coverage == "partial"
    assert "fixture device offline" in result.errors


def test_sentiment_uses_same_probe_reading_contract(remote):
    fetcher = ProbeSentimentFetcher(uuid.uuid4(), remote, uuid.uuid4(), 1)
    url, html = fetcher.fetch("https://weibo.com/example")
    assert url == "https://weibo.com/example" and "岗位职责" in html
    fetcher.close()
    assert RemoteFixture.instances[-1].closed
