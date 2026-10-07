"""Contract fixtures follow observed public fields, never call real models or sites."""
import json
from contextlib import ExitStack

import pytest

from app.collection import CollectionError, JobDocument
from app.adapter_maintenance import KINDS, builtin_manifest, use_manifest
from app.platform_collectors import collect_platform, expected_company_id, extract_jobs
from app.platform_documents import (
    check_platform_page, collect_public_platform, document_detail, document_jobs, initial_state,
    program_detail_payload,
)


@pytest.fixture(autouse=True)
def fixed_contracts():
    with ExitStack() as stack:
        for kind in KINDS:
            stack.enter_context(use_manifest(kind, builtin_manifest(kind)))
        yield


def embedded(payload, body=""):
    return '<html><body>' + body + '<script>__INITIAL_STATE__=' + json.dumps(payload) + ';</script></body></html>'


def zhao_listing():
    return embedded({"onlinePositions": [{
        "number": "CC123J456", "name": "研发工程师", "companyNumber": "CZ123",
        "companyName": "甲公司", "workCity": "上海", "salary60": "20-30K",
        "workingExp": "3-5年", "education": "本科", "staffCard": {"phone": "not retained"},
    }]})


def zhao_detail(**changes):
    detail = {"detailedPosition": {
        "positionNumber": "CC123J456", "positionName": "研发工程师", "jobDesc": "<p>负责系统研发</p>",
        "positionWorkCity": "上海", "salary": "20-30K", "positionPublishTime": "2026-10-04 09:00:00",
    }, "detailedCompany": {"companyName": "甲公司", "companyNumber": "CZ123"}}
    detail.update(changes)
    return embedded({"jobDetail": detail})


BINDING = {"entry_url": "https://www.zhaopin.com/companydetail/CZ123.htm"}
IDENTITY = {"name": "甲公司"}


class Pages:
    def __init__(self, pages):
        self.pages, self.requests = pages, []

    def fetch(self, url):
        self.requests.append(url)
        value = self.pages[url]
        return value if isinstance(value, tuple) else (url, value)

    def close(self):
        pass


@pytest.mark.parametrize(("kind", "url", "expected"), [
    ("zhaopin", "https://www.zhaopin.com/companydetail/CZ123.htm", "CZ123"),
    ("zhaopin", "https://www.zhaopin.com/companydetail/jobs-CZL123/?p=2", "CZL123"),
    ("zhaopin", "https://company.zhaopin.com/CZ123.htm", "CZ123"),
    ("zhaopin", "https://www.zhaopin.com/jobdetail/CC123J456.htm", None),
    ("51job", "https://jobs.51job.com/all/co123.html", "123"),
])
def test_platform_company_identifier(kind, url, expected):
    assert expected_company_id(kind, {"entry_url": url}) == expected


def test_zhaopin_real_field_contract_and_privacy():
    jobs, recognized, count = document_jobs(zhao_listing(), "zhaopin", BINDING, IDENTITY)
    assert recognized and count == 1
    doc, url = jobs[0]
    assert doc.title == "研发工程师" and doc.salary_raw == "20-30K"
    assert doc.locations == ["上海"] and doc.education == "本科"
    assert url == "https://www.zhaopin.com/jobdetail/CC123J456.htm"
    assert "not retained" not in doc.model_dump_json()
    assert not document_jobs(zhao_listing(), "zhaopin", {**BINDING, "external_company_id": "CZ999"}, IDENTITY)[0]


def test_embedded_state_only_decodes_json_and_does_not_execute_scripts():
    assert initial_state('<script>__INITIAL_STATE__=dangerousFunction()</script>') == {}
    assert initial_state('<script>let x="__INITIAL_STATE__={}"</script>') == {}


@pytest.mark.parametrize("html,kind,code", [
    (zhao_detail(guestJobDescClamp={"visibleLines": 5, "totalLines": 10}), "zhaopin", "waiting_login"),
    ('<h1>登录查看完整内容</h1>', "zhaopin", "waiting_login"),
    ('<meta name="aliyun_waf_aa" content="challenge"><script>challenge()</script>', "51job", "waiting_verification"),
])
def test_hidden_or_truncated_data_is_not_a_login_workaround(html, kind, code):
    with pytest.raises(CollectionError) as exc:
        check_platform_page(html, kind)
    assert exc.value.code == code


def test_zhaopin_public_collection_executes_company_list_and_detail(monkeypatch):
    pages = Pages({
        "https://www.zhaopin.com/companydetail/jobs-CZ123/": zhao_listing(),
        "https://www.zhaopin.com/jobdetail/CC123J456.htm": zhao_detail(),
    })
    monkeypatch.setattr("app.platform_documents.PublicFetcher", lambda: pages)
    result = collect_platform(None, "zhaopin", BINDING, IDENTITY, {"max_pages": 1, "max_details": 1})
    assert len(result.records) == 1 and result.coverage == "partial"
    assert result.records[0].description == "负责系统研发"
    assert len(pages.requests) == 2
    assert "staffCard" not in result.documents[0][1]


@pytest.mark.parametrize("changes,code", [
    ({"detailedCompany": {"companyName": "乙公司", "companyNumber": "CZ999"}}, "identity_review"),
    ({"detailedPosition": {"positionNumber": "CC123J999", "jobDesc": "职责"}}, "source_contract_changed"),
    ({"detailedPosition": {"positionNumber": "CC123J456", "jobDesc": ""}}, "source_contract_changed"),
])
def test_zhaopin_detail_rechecks_identity_and_content(changes, code):
    doc = document_jobs(zhao_listing(), "zhaopin", BINDING, IDENTITY)[0][0][0]
    with pytest.raises(CollectionError) as exc:
        document_detail(zhao_detail(**changes), "zhaopin", doc, BINDING, IDENTITY)
    assert exc.value.code == code


def test_51job_public_frontend_response_contract():
    payload = {"resultbody": {"items": [{"jobListItemVO": {
        "jobId": "123456789", "jobName": "产品经理", "coId": 123, "companyName": "甲公司",
        "provideSalaryString": "2-3万", "jobAreaString": "上海", "workYearString": "3年",
        "degreeString": "本科", "hrefAreaPinYin": "shanghai", "hrName": "not retained",
    }}]}}
    binding = {"entry_url": "https://jobs.51job.com/all/co123.html"}
    jobs, recognized, count = extract_jobs(payload, "51job", binding, IDENTITY)
    assert recognized and count == 1 and len(jobs) == 1
    doc, url = jobs[0]
    assert url == "https://jobs.51job.com/shanghai/123456789.html"
    assert doc.salary_raw == "2-3万" and "not retained" not in doc.model_dump_json()
    assert not extract_jobs(payload, "51job", {**binding, "external_company_id": "999"}, IDENTITY)[0]
    payload["resultbody"]["items"][0]["jobListItemVO"].pop("hrefAreaPinYin")
    assert not extract_jobs(payload, "51job", binding, IDENTITY)[0]


def test_51job_dom_card_and_company_detail(monkeypatch):
    tracking = json.dumps({"jobId": "123456789", "jobTitle": "产品经理", "companyId": "123",
                           "jobSalary": "2-3万", "jobArea": "上海"})
    listing = "<div class='card' sensorsdata='" + tracking + "'>" + (
        '<a href="https://jobs.51job.com/shanghai/123456789.html">产品经理</a>'
        '<span class="company-name">甲公司</span></div>')
    detail = '<div class="com_name"><a href="https://jobs.51job.com/all/co123.html">甲公司</a></div>'
    detail += '<h1>产品经理</h1><div class="job_msg">负责产品规划与需求分析</div>'
    binding = {"entry_url": "https://jobs.51job.com/all/co123.html"}
    pages = Pages({binding["entry_url"]: listing, "https://jobs.51job.com/shanghai/123456789.html": detail})
    monkeypatch.setattr("app.platform_documents.PublicFetcher", lambda: pages)
    result = collect_platform(None, "51job", binding, IDENTITY, {"max_pages": 1, "max_details": 1})
    assert len(result.records) == 1 and result.coverage == "partial"
    assert result.records[0].description == "负责产品规划与需求分析"


def test_platform_public_redirect_cannot_escape_source(monkeypatch):
    pages = Pages({"https://www.zhaopin.com/companydetail/jobs-CZ123/":
                   ("https://other.example.com/jobs", zhao_listing())})
    monkeypatch.setattr("app.platform_documents.PublicFetcher", lambda: pages)
    with pytest.raises(CollectionError) as exc:
        collect_public_platform("zhaopin", BINDING, IDENTITY, {"max_pages": 1, "max_details": 1})
    assert exc.value.code == "source_host_mismatch"


def test_51job_detail_needs_verified_company_even_with_description():
    doc = JobDocument(source_job_id="123", title="工程师", url="https://jobs.51job.com/shanghai/123.html")
    with pytest.raises(CollectionError) as exc:
        document_detail('<div class="job_msg">职责</div>', "51job", doc,
                        {"entry_url": "https://jobs.51job.com/all/co456.html"}, IDENTITY)
    assert exc.value.code == "identity_review"


@pytest.mark.parametrize("changes,code", [
    ({"guestJobDescClamp": {"visibleLines": 5, "totalLines": 10}}, "waiting_login"),
    ({"detailedCompany": {"companyName": "乙公司", "companyNumber": "CZ999"}}, "identity_review"),
    ({"detailedPosition": {"positionNumber": "CC123J999", "jobDesc": "职责"}}, "identity_review"),
])
def test_program_input_cannot_bypass_zhaopin_access_or_job_identity(changes, code):
    doc = document_jobs(zhao_listing(), "zhaopin", BINDING, IDENTITY)[0][0][0]
    with pytest.raises(CollectionError) as exc:
        program_detail_payload(zhao_detail(**changes), "zhaopin", doc, BINDING, IDENTITY)
    assert exc.value.code == code


def test_program_receives_original_verified_zhaopin_description_before_normalization():
    doc = document_jobs(zhao_listing(), "zhaopin", BINDING, IDENTITY)[0][0][0]
    payload = program_detail_payload(zhao_detail(), "zhaopin", doc, BINDING, IDENTITY)
    assert payload["verified_details"][0]["jobDesc"] == "<p>负责系统研发</p>"
    assert payload["verified_details"][0]["positionNumber"] == doc.source_job_id


def test_51job_public_redirect_to_same_company_other_job_is_rejected(monkeypatch):
    tracking = json.dumps({"jobId": "123456789", "jobTitle": "产品经理", "companyId": "123"})
    listing = "<div class='card' sensorsdata='" + tracking + "'>" + (
        '<a href="https://jobs.51job.com/shanghai/123456789.html">产品经理</a>'
        '<span class="company-name">甲公司</span></div>')
    detail = '<div class="com_name"><a href="https://jobs.51job.com/all/co123.html">甲公司</a></div>'
    detail += '<h1>另一个岗位</h1><div class="job_msg">另一个岗位的职责正文</div>'
    binding = {"entry_url": "https://jobs.51job.com/all/co123.html"}
    pages = Pages({binding["entry_url"]: listing,
        "https://jobs.51job.com/shanghai/123456789.html": ("https://jobs.51job.com/beijing/987654321.html", detail)})
    monkeypatch.setattr("app.platform_documents.PublicFetcher", lambda: pages)
    with pytest.raises(CollectionError) as error:
        collect_public_platform("51job", binding, IDENTITY, {"max_pages": 1, "max_details": 1})
    assert error.value.code == "identity_review"
