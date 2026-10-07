"""Mock program outputs only: no candidate module is imported or executed here."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from app.adapter_program import (
    MAX_INPUT, prepare_program_input, validate_detail_output, validate_list_output, validate_navigation,
)
from app.collection import CollectionError, JobDocument

BINDING = {"entry_url": "https://www.zhipin.com/gongsi/acme.html", "external_company_id": "acme"}
IDENTITY = {"name": "甲公司"}
PAGE = "https://www.zhipin.com/web/geek/job?page=1"


def claim(value, pointer=None, selector=None, **kwargs):
    return {"value": value, "evidence": {("pointer" if pointer is not None else "selector"): pointer if pointer is not None else selector, **kwargs}}


def output(context, **kwargs):
    request = context.request
    return {"protocol": request["protocol"], "input_id": request["input_id"], "operation": request["operation"], **kwargs}


def json_case(**options):
    row = {"position": {"key": "job:job1", "title": "研发工程师"},
           "company": {"name": "甲公司", "id": "acme"}, "salary": "20-30K", "cities": ["上海", "北京"],
           "securityId": "private-navigation", "lid": "private-tracking", "bossName": "私人招聘者"}
    row.update(options)
    payload = {"account": {"name": "私有账户", "email": "person@example.com"},
               "data": {"rows": [{"userInfo": {"secret": "private-user"}}, row]}}
    context = prepare_program_input("boss", BINDING, IDENTITY, operation="parse_list", page_url=PAGE, payload=payload)
    record = {"row": {"document_id": "json", "pointer": "/data/rows/1"}, "fields": {
        "id": claim("job1", "/position/key", start=4, end=8), "title": claim("研发工程师", "/position/title"),
        "company": claim("甲公司", "/company/name"), "company_id": claim("acme", "/company/id"),
        "salary": claim(row["salary"], "/salary"),
    }, "locations": [claim("上海", "/cities/0"), claim("北京", "/cities/1")]}
    return context, output(context, records=[record])


HTML = '''<html><header>账号信息</header><div class="account-panel">私有账户 person@example.com</div>
<main id="jobs"><article class="job-card" data-job-id="job1" data-company-id="acme" onclick="steal()">
<a class="job-title" href="/job_detail/job1.html?securityId=private-navigation">研发工程师</a>
<span class="company">甲公司</span><input value="private-input"></article>
<article class="job-card" data-job-id="job2" data-company-id="other">
<a class="job-title" href="/job_detail/job2.html">另一岗位</a><span class="company">乙公司</span></article></main>
<div class="pagination"><a rel="next" href="?page=2">下一页</a>
<a rel="next" href="?delete=account">下一页</a><a href="https://evil.example/job_detail/job3.html">其他站点</a></div>
<script>token="private-script"</script><!-- private-comment --></html>'''


def dom_case(operation="parse_list"):
    context = prepare_program_input("boss", BINDING, IDENTITY, operation=operation, page_url=PAGE, html=HTML)
    links = context.request["links"]
    job_link = next(link["id"] for link in links if link["label"] == "研发工程师")
    next_link = next(link["id"] for link in links if link["purpose"] == "next")
    record = {"row": {"document_id": "dom", "selector": ".job-card:nth-of-type(1)"}, "fields": {
        "id": claim("job1", selector="", attribute="data-job-id"),
        "title": claim("研发工程师", selector=".job-title"),
        "company": claim("甲公司", selector=".company"),
        "company_id": claim("acme", selector="", attribute="data-company-id"),
    }, "job_link_id": job_link}
    return context, record, next_link


def test_json_input_redacts_private_material_and_preserves_array_indexes():
    context, result = json_case()
    request = context.request
    serialized = json.dumps(request, ensure_ascii=False)
    assert not any(value in serialized for value in ["private-", "私人招聘者", "私有账户", "person@example.com"])
    rows = request["documents"][0]["content"]["data"]["rows"]
    assert len(rows) == 2 and rows[0] == {} and rows[1]["position"]["key"] == "job:job1"
    request["documents"][0]["content"]["data"]["rows"][1]["position"]["title"] = "篡改副本"
    validated = validate_list_output(context, result)
    assert validated.recognized and validated.raw_count == 1
    assert validated.records[0].title == "研发工程师"
    assert validated.records[0].locations == ["上海", "北京"]
    assert validated.records[0].observed_at is None
    assert validated.records[0].url == "https://www.zhipin.com/job_detail/job1.html"
    assert "private-navigation" in validated.jobs[0][1]
    assert "private-" not in validated.records[0].model_dump_json()
    assert "private-" not in repr(context) and "private-" not in repr(validated)


def test_private_json_key_variants_are_removed_without_renaming_public_keys():
    payload = {"data-set": {"jobs": [{"user_info": {"name": "private-account"}}, {
        "job_id": "job1", "job_title": "研发工程师", "company_id": "acme", "company_name": "甲公司",
        "user_research": "保留岗位的用户研究职责", "security_id": "private-navigation", "boss_name": "private-recruiter",
        "user-profile": {"name": "private-profile"}, "access-key": "private-access", "__session_token__": "private-session",
    }]}}
    context = prepare_program_input("boss", BINDING, IDENTITY, operation="parse_list", page_url=PAGE, payload=payload)
    serialized = json.dumps(context.request, ensure_ascii=False)
    assert "private-" not in serialized
    rows = context.request["documents"][0]["content"]["data-set"]["jobs"]
    assert len(rows) == 2 and rows[0] == {}
    assert rows[1] == {"job_id": "job1", "job_title": "研发工程师", "company_id": "acme", "company_name": "甲公司",
                       "user_research": "保留岗位的用户研究职责"}
    result = output(context, records=[{"row": {"document_id": "json", "pointer": "/data-set/jobs/1"}, "fields": {
        "id": claim("job1", "/job_id"), "title": claim("研发工程师", "/job_title"),
        "company": claim("甲公司", "/company_name"), "company_id": claim("acme", "/company_id")}}])
    assert validate_list_output(context, result).records[0].title == "研发工程师"
    assert payload["data-set"]["jobs"][1]["security_id"] == "private-navigation"


def test_first_party_company_id_wins_over_a_matching_name():
    context, result = json_case(company={"name": "甲公司", "id": "other"})
    result["records"][0]["fields"]["company_id"]["value"] = "other"
    validated = validate_list_output(context, result)
    assert not validated.records and not validated.recognized
    assert validated.rejected_company_count == 1


def test_company_id_cannot_be_cut_out_of_a_different_company_identifier():
    context, result = json_case(company={"name": "甲公司", "id": "other-acme"})
    result["records"][0]["fields"]["company_id"] = claim("acme", "/company/id", start=6, end=10)
    with pytest.raises(CollectionError):
        validate_list_output(context, result)


@pytest.mark.parametrize("change", ["invented_value", "other_row", "broad_row", "invalid_pointer", "slice_outside", "duplicate"])
def test_unproven_or_cross_record_json_claims_are_rejected(change):
    context, result = json_case()
    record = result["records"][0]
    if change == "invented_value":
        record["fields"]["title"]["value"] = "未经观察的高薪岗位"
    elif change == "other_row":
        record["fields"]["company_id"]["evidence"]["pointer"] = "/data/rows/0/company/id"
    elif change == "broad_row":
        record["row"]["pointer"] = ""
    elif change == "invalid_pointer":
        record["fields"]["id"]["evidence"]["pointer"] = "/position/~2key"
    elif change == "slice_outside":
        record["fields"]["id"]["evidence"]["end"] = 999
    else:
        result["records"].append(deepcopy(record))
    with pytest.raises(CollectionError):
        validate_list_output(context, result)


@pytest.mark.parametrize("field", ["url", "observed_at", "coverage", "passed", "headers", "code"])
def test_program_cannot_authorize_urls_observation_times_coverage_or_passed(field):
    context, result = json_case()
    result[field] = "untrusted"
    with pytest.raises(CollectionError):
        validate_list_output(context, result)


def test_dom_evidence_and_opaque_links_keep_navigation_secrets_private():
    context, record, next_link = dom_case()
    request = json.dumps(context.request, ensure_ascii=False)
    assert not any(value in request for value in ["private-", "person@example.com", "steal", "evil.example", "delete=account"])
    validated = validate_list_output(context, output(context, records=[record], navigation=[
        {"type": "follow", "link_id": next_link}, {"type": "scroll", "pixels": 500}, {"type": "wait", "milliseconds": 200}]))
    assert validated.records[0].source_job_id == "job1"
    assert validated.navigation[0].url == "https://www.zhipin.com/web/geek/job?page=2"
    assert "private-navigation" in validated.jobs[0][1]
    assert "?" not in validated.records[0].url


def test_recruiter_regions_are_removed_without_removing_job_description():
    html = '''<div class="boss-card"><span class="boss-name">private-recruiter-name</span></div>
    <div class="hr-info">private-hr-info</div><div class="user-card">private-account-name</div>
    <section class="job-description">岗位职责：负责用户研究与招聘产品研发</section>'''
    context = prepare_program_input("boss", BINDING, IDENTITY, operation="navigation", page_url=PAGE, html=html)
    request = json.dumps(context.request, ensure_ascii=False)
    assert "private-" not in request
    assert "岗位职责：负责用户研究与招聘产品研发" in request


def test_saved_candidate_entrypoints_are_validated_as_inert_source_only():
    from pathlib import Path
    from app.adapter_code_package import validate_candidate_spec

    root = Path(__file__).resolve().parents[2]
    for name in ("boss_reference_v1.py", "boss_reference_v2.py"):
        source = (root / "adapters" / "code" / name).read_text()
        spec = validate_candidate_spec({"schema_version": 1, "contract": "python_adapter_v1", "kind": "boss",
                                        "files": {"adapter.py": source}, "dependencies": []})
        assert spec["files"]["adapter.py"] == source


def test_broad_dom_region_cannot_join_one_job_id_to_another_company():
    context, record, _ = dom_case()
    record["row"]["selector"] = "#jobs"
    record["fields"]["id"] = claim("job2", selector=".job-card:nth-of-type(2)", attribute="data-job-id")
    record["fields"]["title"] = claim("另一岗位", selector=".job-card:nth-of-type(2) .job-title")
    record["fields"]["company"] = claim("甲公司", selector=".job-card:nth-of-type(1) .company")
    record["fields"]["company_id"] = claim("acme", selector=".job-card:nth-of-type(1)", attribute="data-company-id")
    with pytest.raises(CollectionError):
        validate_list_output(context, output(context, records=[record]))


def test_job_link_must_match_the_verified_record_identifier():
    context, record, _ = dom_case()
    record["job_link_id"] = next(link["id"] for link in context.request["links"] if link["label"] == "另一岗位")
    with pytest.raises(CollectionError):
        validate_list_output(context, output(context, records=[record]))


@pytest.mark.parametrize("action", [
    {"type": "click", "selector": "button"}, {"type": "follow", "url": "https://www.zhipin.com/apply"},
    {"type": "follow", "link_id": "invented"}, {"type": "wait", "milliseconds": True},
    {"type": "scroll", "pixels": "100"}, {"type": "scroll", "pixels": -100},
    {"type": "follow", "link_id": "link", "method": "POST", "headers": {}},
])
def test_navigation_cannot_grant_new_actions_or_network_authority(action):
    context, _, _ = dom_case("navigation")
    with pytest.raises(CollectionError):
        validate_navigation(context, output(context, navigation=[action]))


def test_navigation_has_total_budgets_and_cannot_follow_unmatched_company_jobs():
    context, _, next_link = dom_case("navigation")
    result = output(context, navigation=[{"type": "follow", "link_id": next_link}])
    assert validate_navigation(context, result)[0].type == "follow"
    for actions in ([{"type": "wait", "milliseconds": 1500}] * 3,
                    [{"type": "scroll", "pixels": 1500}] * 3,
                    [{"type": "follow", "link_id": next_link}] * 2,
                    [{"type": "follow", "link_id": next(item["id"] for item in context.request["links"] if item["purpose"] == "job")} ]):
        with pytest.raises(CollectionError):
            validate_navigation(context, output(context, navigation=actions))


def test_detail_cannot_change_identity_and_must_quote_original_body():
    observed = datetime(2026, 10, 5, tzinfo=timezone.utc)
    job = JobDocument(source_job_id="job1", title="工程师", url="https://www.zhipin.com/job_detail/job1.html", observed_at=observed)
    context = prepare_program_input("boss", BINDING, IDENTITY, operation="parse_detail", page_url=job.url,
                                    html='<div class="jd-v2"><p class="body">负责平台研发 与 性能优化</p><p class="requirements">熟悉数据库</p></div>', job=job)
    result = output(context, row={"document_id": "dom", "selector": ".jd-v2"}, fields={
        "description": claim("负责平台研发\n与 性能优化", selector=".body"),
        "requirements": claim("熟悉数据库", selector=".requirements"),
    })
    parsed, actions = validate_detail_output(context, result)
    assert parsed.source_job_id == job.source_job_id and parsed.url == job.url and parsed.observed_at == observed
    assert parsed.description == "负责平台研发 与 性能优化" and not actions
    assert job.description == ""
    result["fields"]["description"]["value"] = "编造完整岗位内容"
    with pytest.raises(CollectionError):
        validate_detail_output(context, result)


def test_empty_list_is_unrecognized_and_replayed_output_is_rejected():
    context, result = json_case()
    assert not validate_list_output(context, output(context, records=[])).recognized
    result["input_id"] = "different-input"
    with pytest.raises(CollectionError):
        validate_list_output(context, result)


def test_identical_documents_have_stable_input_and_opaque_link_ids():
    first, _, _ = dom_case()
    second, _, _ = dom_case()
    assert first.request == second.request
    assert first.request["input_id"].startswith("input_")
    assert all(link["id"].startswith("link_") for link in first.request["links"])
    other = prepare_program_input("boss", BINDING, IDENTITY, operation="navigation", page_url=PAGE, html=HTML)
    assert other.request["input_id"] != first.request["input_id"]
    changed = prepare_program_input("boss", BINDING, IDENTITY, operation="parse_list", page_url=PAGE, html=HTML.replace("研发工程师", "测试工程师"))
    assert changed.request["input_id"] != first.request["input_id"]


def test_json_pointer_escaping_and_numeric_public_ids_are_supported():
    context = prepare_program_input("liepin", {"entry_url": "https://www.liepin.com/company/123/"}, IDENTITY,
                                    operation="parse_list", page_url="https://www.liepin.com/company/123/",
                                    payload={"rows": [{"id/value": 999, "title~text": "工程师", "co": 123}]})
    result = output(context, records=[{"row": {"document_id": "json", "pointer": "/rows/0"}, "fields": {
        "id": claim("999", "/id~1value"), "title": claim("工程师", "/title~0text"), "company_id": claim("123", "/co")}}])
    assert validate_list_output(context, result).records[0].url == "https://www.liepin.com/job/999.shtml"


def test_input_and_output_limits_and_login_page_are_enforced():
    with pytest.raises(CollectionError):
        prepare_program_input("boss", BINDING, IDENTITY, operation="parse_list", page_url=PAGE, html="x" * (MAX_INPUT + 1))
    with pytest.raises(CollectionError):
        prepare_program_input("boss", BINDING, IDENTITY, operation="parse_list", page_url=PAGE, html="请先登录")
    context, result = json_case()
    result["records"] *= 101
    with pytest.raises(CollectionError):
        validate_list_output(context, result)
