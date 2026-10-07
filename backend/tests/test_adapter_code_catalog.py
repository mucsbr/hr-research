from copy import deepcopy
import json

import pytest

from app.adapter_code_catalog import catalog_for, failure_case
from app.adapter_program import prepare_program_input, validate_detail_output, validate_list_output
from app.collection import CollectionError, JobDocument


@pytest.mark.parametrize("kind", ["boss", "liepin", "zhaopin", "51job", "official"])
def test_production_catalog_has_independent_three_operation_oracles_without_executing_source(kind):
    catalog = catalog_for(kind)
    assert {case["operation"] for case in catalog["evidence_cases"]} == {"parse_list", "parse_detail", "navigation"}
    assert "QA" not in catalog["base_spec"]["files"]["adapter.py"]
    assert "from lib.rules import KIND, RULES" in catalog["base_spec"]["files"]["adapter.py"]
    assert catalog["base_spec"]["dependencies"] == ["beautifulsoup4==4.13.5"]
    assert catalog == catalog_for(kind)


def test_client_expected_never_becomes_list_or_detail_oracle():
    from app.adapter_maintenance import builtin_manifest
    binding = {"entry_url": "https://www.zhipin.com/gongsi/acme.html"}
    identity = {"name": "甲公司"}
    evidence = {"type": "response_list", "expected": [{"title": "客户伪造的真值"}], "payload": {"changed": [{
        "encryptJobId": "job1", "jobName": "实际原文工程师", "brandName": "甲公司", "encryptBrandId": "acme"}]}}
    case = failure_case("boss", binding, identity, evidence, builtin_manifest("boss"))
    assert case["expected"]["records"][0]["fields"]["title"]["value"] == "实际原文工程师"
    assert "客户伪造的真值" not in json.dumps(case, ensure_ascii=False)
    text = "岗位职责：负责平台研发与系统架构设计，维护稳定的数据处理流程，参与接口开发和性能优化，与产品团队协作完成迭代。"
    detail = {"type": "detail_dom", "html": '<div class="new-jd"><h2 class="heading">岗位职责</h2><p>' + text + '</p></div>',
              "expected": {"source_job_id": "fake-job", "title": "fake-title"}}
    case = failure_case("boss", binding, identity, detail, builtin_manifest("boss"))
    assert case["expected"]["row"]["selector"] == ".new-jd"
    assert case["expected"]["fields"]["description"]["value"].endswith(text)
    assert "fake-title" not in json.dumps(case)


def test_unknown_field_meanings_do_not_create_an_oracle():
    from app.adapter_maintenance import builtin_manifest
    with pytest.raises(CollectionError) as exc:
        failure_case("boss", {"entry_url": "https://www.zhipin.com/gongsi/acme.html"}, {"name": "甲公司"},
                     {"type": "response_list", "payload": {"new": [{"unknown": "possible job"}]}, "expected": [{"title": "fake"}]}, builtin_manifest("boss"))
    assert exc.value.code == "code_oracle_unavailable"


def official_context(item):
    binding = {"kind": "careers", "entry_url": "https://careers.example.com/jobs"}
    context = prepare_program_input("official", binding, {"name": "甲公司"}, operation="parse_list", page_url=binding["entry_url"],
                                    html='<script type="application/ld+json">' + json.dumps(item, ensure_ascii=False) + '</script>')
    return context


def official_output(context, **fields):
    return {key: context.request[key] for key in ("protocol", "input_id", "operation")} | fields


def test_official_url_id_preserves_native_identity_and_org_is_checked_even_if_omitted_by_candidate():
    item = {"@type": "JobPosting", "title": "工程师", "url": "https://careers.example.com/jobs/1", "hiringOrganization": {"name": "甲公司"}}
    context = official_context(item)
    record = {"row": {"document_id": "json", "pointer": "/blocks/0"}, "job_link_id": context.request["links"][0]["id"],
              "fields": {"title": {"value": "工程师", "evidence": {"pointer": "/title"}}}}
    result = validate_list_output(context, official_output(context, records=[record]))
    assert result.records[0].source_job_id == item["url"][:500]
    bad = official_context({**item, "hiringOrganization": {"name": "乙公司"}})
    record["job_link_id"] = bad.request["links"][0]["id"]
    assert not validate_list_output(bad, official_output(bad, records=[record])).records
    record["fields"]["id"] = {"value": "made-up", "evidence": {"pointer": "/title"}}
    with pytest.raises(CollectionError):
        validate_list_output(context, official_output(context, records=[record]))


def test_official_foreign_urls_and_unsaved_css_plans_do_not_grant_navigation():
    context = official_context({"@type": "JobPosting", "title": "工程师", "url": "https://other.example/jobs/1"})
    assert context.request["links"] == []
    with pytest.raises(CollectionError):
        prepare_program_input("official", {"kind": "careers", "entry_url": "https://careers.example.com/jobs"}, {"name": "甲公司"},
                              operation="parse_list", page_url="https://other.example/jobs", html="<p>jobs</p>")
    with pytest.raises(CollectionError):
        prepare_program_input("official", {"kind": "careers", "entry_url": "https://careers.example.com/jobs",
                              "config": {"website_plan": {"job_link_selector": "a.job"}}}, {"name": "甲公司"},
                              operation="parse_list", page_url="https://careers.example.com/jobs", html="<p>jobs</p>")


def test_official_detail_must_belong_to_current_observed_job():
    binding = {"kind": "careers", "entry_url": "https://careers.example.com/jobs"}
    job = JobDocument(source_job_id="role-1", title="工程师", url="https://careers.example.com/jobs/1")
    html = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "工程师", "description": "负责研发",
            "url": "https://careers.example.com/jobs/2", "hiringOrganization": {"name": "甲公司"}}, ensure_ascii=False) + '</script>'
    context = prepare_program_input("official", binding, {"name": "甲公司"}, operation="parse_detail", page_url=job.url, html=html, job=job)
    expected = official_output(context, row={"document_id": "json", "pointer": "/blocks/0"}, fields={
        "description": {"value": "负责研发", "evidence": {"pointer": "/description"}}})
    with pytest.raises(CollectionError):
        validate_detail_output(context, expected)


def test_51job_tracking_rows_use_first_party_observed_url_job_id_without_invented_area():
    catalog = catalog_for("51job")
    case = next(case for case in catalog["evidence_cases"] if case["id"] == "observed-tracking-list")
    assert case["input"]["links"][0]["job_id"] == "88888888"
    record = case["expected"]["records"][0]
    assert record["job_link_id"] == case["input"]["links"][0]["id"]
    assert "url_area" not in record["fields"]
    changed = deepcopy(case)
    changed["expected"]["records"][0]["job_link_id"] = "unobserved"
    assert changed != case


def test_description_oracle_ignores_verified_title_but_rejects_two_independent_semantic_regions():
    from app.adapter_maintenance import builtin_manifest
    body = "岗位职责：负责企业平台研发、数据处理与接口设计，持续优化服务可靠性和用户体验。"
    binding = {"entry_url": "https://www.zhipin.com/gongsi/acme.html"}
    evidence = {"type": "detail_dom", "html": '<h1>工程师</h1><p class="description">' + body + '</p>'}
    case = failure_case("boss", binding, {"name": "甲公司"}, evidence, builtin_manifest("boss"))
    assert case["expected"]["row"]["selector"] == ".description"
    assert case["expected"]["fields"]["description"]["value"] == body
    evidence["html"] = '<main class="wrapper">' + evidence["html"] + '<p class="another">' + body + '另一岗位。</p></main>'
    with pytest.raises(CollectionError, match="职责原文不存在唯一语义区域"):
        failure_case("boss", binding, {"name": "甲公司"}, evidence, builtin_manifest("boss"))


def test_production_catalog_protects_raw_tencent_and_zhaopin_branches():
    cases = {case["id"]: case for case in catalog_for("official")["evidence_cases"]}
    assert {"tencent-raw-list", "tencent-raw-detail", "tencent-verified-detail", "tencent-fixed-api-navigation"} <= cases.keys()
    assert cases["tencent-raw-list"]["input"]["board"] == "tencent"
    assert cases["tencent-raw-detail"]["expected"]["fields"]["description"]["value"].startswith("<p>")
    assert cases["tencent-raw-detail"]["expected"]["fields"]["company"]["value"] == "腾讯科技（深圳）有限公司"
    detail = next(case for case in catalog_for("zhaopin")["evidence_cases"] if case["id"] == "verified-json-detail")
    assert detail["expected"]["row"]["pointer"] == "/verified_details/0"
    assert detail["expected"]["fields"]["description"]["value"].startswith("<p>")


def test_tencent_raw_identity_and_metadata_are_first_party_owned():
    from app.adapter_code_catalog import tencent_oracle
    binding = {"kind": "careers", "adapter": "tencent", "entry_url": "https://careers.tencent.com/search.html"}
    identity = {"name": "腾讯"}
    row = {"PostId": "12345", "RecruitPostName": "平台研发工程师", "ComName": "腾讯科技（深圳）有限公司"}
    context = prepare_program_input("official", binding, identity, operation="parse_list",
        page_url="https://careers.tencent.com/tencentcareer/api/post/Query?pageIndex=1&pageSize=10",
        payload={"Code": 200, "Data": {"Count": 1, "Posts": [row]}})
    expected = tencent_oracle(context)
    job = validate_list_output(context, expected).records[0]
    assert job.source_job_id == "12345" and job.recruiting_entity == row["ComName"]
    assert job.url == "https://careers.tencent.com/jobdesc.html?postId=12345"
    detail_url = "https://careers.tencent.com/tencentcareer/api/post/ByPostId?postId=12345"
    with pytest.raises(CollectionError, match="PostId与请求不一致"):
        prepare_program_input("official", binding, identity, operation="parse_detail", page_url=detail_url,
                              payload={"Code": 200, "Data": {**row, "PostId": "other"}}, job=job)
    detail = prepare_program_input("official", binding, identity, operation="parse_detail", page_url=detail_url,
        payload={"Code": 200, "Data": {**row, "Responsibility": "<p>岗位职责：负责数据研发。</p>"}}, job=job)
    output = tencent_oracle(detail)
    assert validate_detail_output(detail, output)[0].description == "岗位职责：负责数据研发。"
    output["fields"]["title"] = {"value": row["ComName"], "evidence": {"pointer": "/ComName"}}
    with pytest.raises(CollectionError, match="腾讯字段必须对应登记原Data字段"):
        validate_detail_output(detail, output)
