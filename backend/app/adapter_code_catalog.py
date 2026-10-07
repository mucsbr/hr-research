"""Server-owned production source and independent evidence oracles; never runs candidates."""
from copy import deepcopy
import hashlib
import json
import re

from bs4 import BeautifulSoup

from app.adapter_code_package import content_sha256, validate_candidate_spec
from app.adapter_program import (
    ProgramContext, _official_plan, _official_url, _public_url, capture_program_failure,
    prepare_program_input, validate_detail_output, validate_list_output, validate_navigation,
)
from app.collection import CollectionError, JobDocument
from app.config import ROOT

CATALOG_VERSION = "production-parser-v1"
SUPPORTED = {"boss", "liepin", "zhaopin", "51job", "official"}


def fail(message):
    raise CollectionError("code_oracle_unavailable", message)


def _rules(kind, manifest=None):
    if kind not in SUPPORTED:
        fail("平台没有登记生产程序与可信案例")
    raw = manifest or json.loads((ROOT / "adapters/manifests" / (kind + ".json")).read_text())
    rules = {key: deepcopy(raw[key]) for key in ("list_paths", "fields", "detail_selectors")}
    if kind == "51job":
        if ["tracking_rows"] not in rules["list_paths"]:
            rules["list_paths"].append(["tracking_rows"])
        for field, key in {"id": "jobId", "title": "jobTitle", "company_id": "companyId", "company": "companyName",
                           "salary": "jobSalary", "location": "jobArea", "experience": "jobYear", "education": "jobDegree"}.items():
            if [key] not in rules["fields"][field]:
                rules["fields"][field].append([key])
    return rules


def production_spec(kind, manifest=None):
    source = (ROOT / "adapters/programs/adapter.py").read_text()
    rules = _rules(kind, manifest)
    return validate_candidate_spec({"schema_version": 1, "contract": "python_adapter_v1", "kind": kind,
        "files": {"adapter.py": source, "lib/rules.py": "KIND = " + repr(kind) + "\nRULES = " + repr(rules) + "\n"},
        "dependencies": ["beautifulsoup4==4.13.5"]})


def _pointer(path):
    return "/" + "/".join(str(key).replace("~", "~0").replace("/", "~1") for key in path)


def _get(value, path):
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _claim(value, **reference):
    return {"value": str(value), "evidence": reference}


def _envelope(context, **fields):
    return {key: context.request[key] for key in ("protocol", "input_id", "operation")} | fields


def _case(identifier, context, expected, *, role="regression"):
    operation = context.request["operation"]
    validator = {"parse_list": validate_list_output, "parse_detail": validate_detail_output, "navigation": validate_navigation}[operation]
    checked = validator(context, expected)
    if operation == "parse_list" and not checked.records:
        fail("原资料没有独立核实的本公司岗位，不能生成代码修复真值")
    return {"id": identifier, "operation": operation, "input": context.request, "expected": expected, "role": role}


def _lists(value, path=(), depth=0):
    if depth > 12:
        return
    if isinstance(value, list):
        if value and any(isinstance(row, dict) for row in value):
            yield path, value
        for index, child in enumerate(value[:100]):
            yield from _lists(child, (*path, index), depth + 1)
    elif isinstance(value, dict):
        for key, child in list(value.items())[:100]:
            yield from _lists(child, (*path, key), depth + 1)


def list_oracle(context, rules, *, search_moved=False):
    """Infer only still-known public field meanings, never new schema semantics from a model."""
    data = next((doc["content"] for doc in context.request["documents"] if doc["id"] == "json"), None)
    options = list(_lists(data))[:30] if search_moved else [(tuple(path), _get(data, path)) for path in rules["list_paths"]]
    for path, rows in options:
        if not isinstance(rows, list) or not rows:
            continue
        records = []
        for index, row in enumerate(rows[:100]):
            if not isinstance(row, dict):
                continue
            fields = {}
            for name, paths in rules["fields"].items():
                for keys in paths:
                    value = _get(row, keys)
                    if type(value) in {str, int, float} and str(value).strip():
                        fields[name] = _claim(value, pointer=_pointer(keys))
                        break
            if {"id", "title"}.issubset(fields):
                record = {"row": {"document_id": "json", "pointer": _pointer([*path, index])}, "fields": fields}
                linked = next((link["id"] for link in context.request["links"] if link.get("job_id") == fields["id"]["value"]), None)
                if linked:
                    record["job_link_id"] = linked
                records.append(record)
        if not records:
            continue
        expected = _envelope(context, records=records, navigation=[])
        try:
            accepted = validate_list_output(context, expected)
        except CollectionError:
            continue
        if accepted.records:
            return expected
    fail("失败资料不能按登记字段含义独立核验，保留人工工程入口")


def _selector(html, *, require_signal=False):
    soup = BeautifulSoup(html, "html.parser")
    def semantic(node):
        value = node.get_text(" ", strip=True)
        return len(value) >= 30 and re.search(r"岗位职责|职位描述|工作职责|任职要求|职位要求", value)
    # A verified title or page navigation is not part of the job description.
    # Independently locate a single semantic region before choosing its selector;
    # an ancestor enclosing two unrelated long descriptions is not an oracle.
    if require_signal:
        leaves = [node for node in soup.find_all(True) if semantic(node)
                  and not any(semantic(child) for child in node.find_all(True))]
        if len(leaves) != 1:
            fail("职责原文不存在唯一语义区域，不能合并多个独立区域为真值")
    candidates = []
    for node in soup.find_all(True):
        text = node.get_text(" ", strip=True)
        if not text or (require_signal and not semantic(node)):
            continue
        choices = []
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", node.get("id", "")):
            choices.append("#" + node["id"])
        choices += ["." + value for value in node.get("class", []) if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", value)]
        for selector in choices:
            if len(soup.select(selector, limit=2)) == 1:
                candidates.append((len(text), selector, text))
    if not candidates:
        fail("没有唯一且可核验的原文职责区域")
    _, selector, text = min(candidates)
    return selector, text


def detail_oracle(context, *, require_signal=False):
    if context.request["kind"] == "zhaopin":
        data = next((doc["content"] for doc in context.request["documents"] if doc["id"] == "json"), {})
        rows = data.get("verified_details", []) if isinstance(data, dict) else []
        if len(rows) == 1 and isinstance(rows[0], dict) and isinstance(rows[0].get("jobDesc"), str) and rows[0]["jobDesc"].strip():
            return _envelope(context, row={"document_id": "json", "pointer": "/verified_details/0"},
                             fields={"description": _claim(rows[0]["jobDesc"], pointer="/jobDesc")}, navigation=[])
    html = next((doc["content"] for doc in context.request["documents"] if doc["id"] == "dom"), None)
    if not html:
        fail("缺少第一方职责原文")
    if not require_signal and context.request.get("plan"):
        selector = context.request["plan"]["description_selector"]
        nodes = BeautifulSoup(html, "html.parser").select(selector, limit=2)
        if len(nodes) != 1:
            fail("保存的CSS计划不能唯一核验职责区域")
        text = nodes[0].get_text(" ", strip=True)
    else:
        selector, text = _selector(html, require_signal=require_signal)
    return _envelope(context, row={"document_id": "dom", "selector": selector},
                     fields={"description": _claim(text, selector="")}, navigation=[])


def _official_cases():
    binding = {"kind": "careers", "entry_url": "https://careers.example.com/jobs"}
    identity = {"name": "甲公司"}
    item = {"@type": "JobPosting", "title": "平台研发工程师", "url": "https://careers.example.com/jobs/dev",
            "identifier": {"value": "role-1"}, "description": "负责平台研发", "hiringOrganization": {"name": "甲公司"}}
    html = '<script type="application/ld+json">' + json.dumps(item, ensure_ascii=False) + '</script>'
    context = prepare_program_input("official", binding, identity, operation="parse_list", page_url=binding["entry_url"], html=html)
    link_id = next(link["id"] for link in context.request["links"] if link["purpose"] == "job")
    expected = _envelope(context, records=[{"row": {"document_id": "json", "pointer": "/blocks/0"}, "fields": {
        "title": _claim("平台研发工程师", pointer="/title"), "company": _claim("甲公司", pointer="/hiringOrganization/name")},
        "job_link_id": link_id}], navigation=[])
    job = validate_list_output(context, expected).records[0]
    cases = [_case("baseline-list", context, expected)]
    detail = prepare_program_input("official", binding, identity, operation="parse_detail", page_url=job.url, html=html, job=job)
    cases.append(_case("baseline-detail", detail, _envelope(detail, row={"document_id": "json", "pointer": "/blocks/0"},
                 fields={"description": _claim("负责平台研发", pointer="/description")}, navigation=[])))
    from app.website_agent import WebsitePlan
    plan = WebsitePlan(job_link_selector="a.job-link", title_selector="h1.job-title", description_selector=".job-description", next_selector="a.next").model_dump()
    binding["config"] = {"website_plan": plan, "plan_hash": hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()}
    css = prepare_program_input("official", binding, identity, operation="parse_list", page_url=binding["entry_url"], html='<a class="job-link" href="/jobs/css">平台研发工程师</a>')
    link_id = css.request["links"][0]["id"]
    expected = _envelope(css, records=[{"row": {"document_id": "dom", "selector": 'a[data-program-link-id="' + link_id + '"]'},
        "fields": {"title": _claim("平台研发工程师", selector="")}, "job_link_id": link_id}], navigation=[])
    job = validate_list_output(css, expected).records[0]
    cases.append(_case("saved-css-list", css, expected))
    detail = prepare_program_input("official", binding, identity, operation="parse_detail", page_url=job.url,
                                   html='<h1 class="job-title">平台研发工程师</h1><div class="job-description">负责平台研发</div>', job=job)
    cases.append(_case("saved-css-detail", detail, detail_oracle(detail)))
    return binding, identity, cases


def regression_cases(kind, manifest=None):
    if kind == "official":
        binding, identity, cases = _official_cases()
        cases.extend(_tencent_cases())
    else:
        from app.adapter_maintenance import _fixture
        binding, identity, payload, html = _fixture(kind)
        context = prepare_program_input(kind, binding, identity, operation="parse_list", page_url=binding["entry_url"], payload=payload)
        expected = list_oracle(context, _rules(kind, manifest))
        job = validate_list_output(context, expected).records[0]
        cases = [_case("baseline-list", context, expected)]
        detail = prepare_program_input(kind, binding, identity, operation="parse_detail", page_url=job.url, html=html, job=job)
        cases.append(_case("baseline-detail", detail, detail_oracle(detail)))
        if kind == "zhaopin":
            detail = prepare_program_input(kind, binding, identity, operation="parse_detail", page_url=job.url,
                payload={"verified_details": [{"jobDesc": "<p>岗位职责：负责平台研发与数据接口维护。</p>"}]}, job=job)
            cases.append(_case("verified-json-detail", detail, detail_oracle(detail)))
        if kind == "51job":
            tracking = prepare_program_input(kind, binding, identity, operation="parse_list", page_url=binding["entry_url"],
                payload={"tracking_rows": [{"jobId": "88888888", "jobTitle": "平台研发工程师", "companyId": "123", "jobSalary": "20-30K", "jobArea": "上海"}]},
                html='<a href="https://jobs.51job.com/shanghai/88888888.html">平台研发工程师</a>')
            cases.append(_case("observed-tracking-list", tracking, list_oracle(tracking, _rules(kind, manifest))))
    nav = prepare_program_input(kind, binding, identity, operation="navigation", page_url=binding["entry_url"],
                                html='<div class="pagination"><a class="next" rel="next" href="?page=2">下一页</a></div>')
    link_id = next(link["id"] for link in nav.request["links"] if link["purpose"] == "next")
    cases.append(_case("baseline-navigation", nav, _envelope(nav, navigation=[{"type": "follow", "link_id": link_id}, {"type": "wait", "milliseconds": 100}])))
    return cases


def catalog_for(kind, manifest=None):
    spec = production_spec(kind, manifest)
    cases = regression_cases(kind, manifest)
    material = {"version": CATALOG_VERSION, "kind": kind, "base_spec": spec, "evidence_cases": cases}
    return {**material, "catalog_sha256": content_sha256(material)}


def _program_context(kind, binding, identity, capsule):
    allowed = {"type", "kind", "operation", "request", "request_sha256", "observed_url", "binding", "captured_links", "capture_sha256"}
    if set(capsule) != allowed or capsule.get("kind") != kind or capsule.get("capture_sha256") != content_sha256({
        key: value for key, value in capsule.items() if key != "capture_sha256"
    }):
        fail("第一方程序故障封套字段或摘要不一致")
    request = capsule["request"]
    if (not isinstance(request, dict) or set(request) - {"protocol", "input_id", "operation", "kind", "documents", "links", "job", "plan", "current_job_link_id", "board"}
            or request.get("protocol") != "adapter-program/1" or request.get("kind") != kind
            or request.get("operation") != capsule["operation"] or capsule["request_sha256"] != content_sha256(request)
            or request.get("input_id") != "input_" + content_sha256({key: value for key, value in request.items() if key != "input_id"})):
        fail("程序故障未绑定稳定原始请求")
    if request.get("board") is not None and not (request["board"] == "tencent" and kind == "official" and binding.get("adapter") == "tencent"):
        fail("程序不能自行声明集团官网权限")
    expected_binding = {"kind": binding.get("kind", kind), "company_id": str(binding["company_id"]) if binding.get("company_id") else None,
                        "entry_url": _official_url(binding["entry_url"]),
                        "external_company_id": str(binding["external_company_id"]) if binding.get("external_company_id") is not None else None}
    if capsule["binding"] != expected_binding or not _public_url(capsule["observed_url"], kind, binding):
        fail("程序故障与当前已授权公司来源不一致")
    if kind == "official" and request.get("plan") != _official_plan(binding):
        # Absence is equivalent to no saved plan; an injected plan is never accepted.
        if request.get("plan") is not None or _official_plan(binding) is not None:
            fail("程序故障CSS计划不是第一方保存的当前计划")
    links = {}
    if not isinstance(capsule["captured_links"], list) or len(capsule["captured_links"]) > 200:
        fail("观察链接超过预算")
    public = {item["id"]: item for item in request.get("links", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
    for entry in capsule["captured_links"]:
        if not isinstance(entry, dict) or set(entry) - {"id", "url", "purpose", "label", "job_id"}:
            fail("观察链接包含未允许字段")
        key = entry.get("id")
        if key in links or key not in public or not _public_url(entry.get("url", ""), kind, binding):
            fail("观察链接不属于原程序请求或来源")
        if {name: value for name, value in entry.items() if name != "url"} != public[key]:
            fail("观察链接与公开opaque引用不一致")
        links[key] = {name: value for name, value in entry.items() if name != "id"}
    if set(links) != set(public):
        fail("程序故障缺少第一方观察链接映射")
    job = None
    if request.get("operation") == "parse_detail":
        data = request.get("job")
        if not isinstance(data, dict) or set(data) != {"id", "title"}:
            fail("详情故障缺少原第一方岗位标识")
        job = JobDocument(source_job_id=data["id"], title=data["title"], url=capsule["observed_url"])
    context = ProgramContext(deepcopy(request), deepcopy(binding), deepcopy(identity), links, {}, job, capsule["observed_url"])
    if capture_program_failure(context) != capsule:
        fail("故障封套不是规范的第一方安全资料，未用于模型")
    return context


def _posting_nodes(value, path=()):
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from _posting_nodes(child, (*path, index))
    elif isinstance(value, dict):
        if value.get("_program_job_link_id"):
            yield path, value
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from _posting_nodes(child, (*path, key))


def official_oracle(context):
    request = context.request
    data = next((item["content"] for item in request["documents"] if item["id"] == "json"), None)
    if request.get("board") == "tencent":
        return tencent_oracle(context)
    if request["operation"] == "parse_list":
        records = []
        for path, row in _posting_nodes(data):
            if not isinstance(row.get("title"), str) or not row["title"].strip():
                continue
            fields = {"title": _claim(row["title"], pointer="/title")}
            organization = row.get("hiringOrganization")
            if isinstance(organization, dict) and isinstance(organization.get("name"), str):
                fields["company"] = _claim(organization["name"], pointer="/hiringOrganization/name")
            elif isinstance(organization, str) and organization.strip():
                fields["company"] = _claim(organization, pointer="/hiringOrganization")
            records.append({"row": {"document_id": "json", "pointer": _pointer(path)}, "fields": fields,
                            "job_link_id": row["_program_job_link_id"]})
        if not records and request.get("plan"):
            html = next((item["content"] for item in request["documents"] if item["id"] == "dom"), "")
            for node in BeautifulSoup(html, "html.parser").select(request["plan"]["job_link_selector"], limit=100):
                ref = node.get("data-program-link-id")
                text = node.get_text(" ", strip=True)
                if ref and text:
                    records.append({"row": {"document_id": "dom", "selector": 'a[data-program-link-id="' + ref + '"]'},
                                    "fields": {"title": _claim(text, selector="")}, "job_link_id": ref})
        return _envelope(context, records=records[:100], navigation=[])
    if request["operation"] == "parse_detail":
        for path, row in _posting_nodes(data):
            if row.get("_program_job_link_id") == request.get("current_job_link_id") and isinstance(row.get("description"), str) and row["description"].strip():
                fields = {"description": _claim(row["description"], pointer="/description")}
                if isinstance(row.get("qualifications"), str) and row["qualifications"].strip():
                    fields["requirements"] = _claim(row["qualifications"], pointer="/qualifications")
                return _envelope(context, row={"document_id": "json", "pointer": _pointer(path)}, fields=fields, navigation=[])
        return detail_oracle(context, require_signal=True)
    fail("没有对应官网字段操作真值")


def tencent_oracle(context):
    request = context.request
    data = next(item["content"] for item in request["documents"] if item["id"] == "json")
    mapping = {"id": "PostId", "title": "RecruitPostName", "company": "ComName", "location": "LocationName",
               "department": "BGName", "function": "CategoryName", "experience": "RequireWorkYearsName"}
    if request["operation"] == "parse_list":
        records = []
        for index, row in enumerate(data.get("Data", {}).get("Posts", [])):
            fields = {name: _claim(row[key], pointer="/" + key) for name, key in mapping.items()
                      if type(row.get(key)) in {str, int, float} and str(row[key]).strip()}
            if {"id", "title"}.issubset(fields) and row.get("_program_job_link_id"):
                records.append({"row": {"document_id": "json", "pointer": f"/Data/Posts/{index}"}, "fields": fields,
                                "job_link_id": row["_program_job_link_id"]})
        return _envelope(context, records=records, navigation=[])
    pointer = "/verified_details/0" if "verified_details" in data else "/Data"
    row = data["verified_details"][0] if "verified_details" in data else data["Data"]
    mapping.pop("id")
    mapping.update(description="Responsibility", requirements="Requirement", source_updated_at="LastUpdateTime")
    fields = {name: _claim(row[key], pointer="/" + key) for name, key in mapping.items() if isinstance(row.get(key), str) and row[key].strip()}
    return _envelope(context, row={"document_id": "json", "pointer": pointer}, fields=fields, navigation=[])


def _tencent_cases():
    binding = {"kind": "careers", "adapter": "tencent", "entry_url": "https://careers.tencent.com/search.html"}
    identity = {"name": "腾讯"}
    url = "https://careers.tencent.com/tencentcareer/api/post/Query?pageIndex=1&pageSize=10&language=zh-cn&area=cn"
    row = {"PostId": "12345", "RecruitPostName": "平台研发工程师", "ComName": "腾讯科技（深圳）有限公司",
           "LocationName": "深圳", "BGName": "技术工程事业群", "CategoryName": "技术", "RequireWorkYearsName": "三年以上"}
    payload = {"Code": 200, "Data": {"Count": 12, "Posts": [row]}}
    context = prepare_program_input("official", binding, identity, operation="parse_list", page_url=url, payload=payload)
    expected = tencent_oracle(context)
    job = validate_list_output(context, expected).records[0]
    cases = [_case("tencent-raw-list", context, expected)]
    row = {**row, "Responsibility": "<p>岗位职责：负责平台研发与数据接口维护。</p>",
           "Requirement": "<p>任职要求：熟悉数据库与软件工程。</p>", "LastUpdateTime": "2026-10-01"}
    detail_url = "https://careers.tencent.com/tencentcareer/api/post/ByPostId?postId=12345&language=zh-cn"
    for name, detail_payload in [("raw", {"Code": 200, "Data": row}), ("verified", {"verified_details": [row]})]:
        detail = prepare_program_input("official", binding, identity, operation="parse_detail", page_url=detail_url, payload=detail_payload, job=job)
        cases.append(_case("tencent-" + name + "-detail", detail, tencent_oracle(detail)))
    nav = prepare_program_input("official", binding, identity, operation="navigation", page_url=url, payload=payload)
    ref = next(link["id"] for link in nav.request["links"] if link["purpose"] == "next")
    cases.append(_case("tencent-fixed-api-navigation", nav, _envelope(nav, navigation=[{"type": "follow", "link_id": ref}, {"type": "wait", "milliseconds": 100}])))
    return cases


def program_failure_case(kind, binding, identity, capsule, manifest):
    context = _program_context(kind, binding, identity, capsule)
    operation = context.request["operation"]
    if operation == "navigation":
        observed_next = next((link["id"] for link in context.request["links"] if link["purpose"] == "next"), None)
        if not observed_next:
            fail("没有独立观察到下一页，不能猜测新的导航计划")
        expected = _envelope(context, navigation=[{"type": "follow", "link_id": observed_next}, {"type": "wait", "milliseconds": 100}])
    elif kind == "official":
        expected = official_oracle(context)
    elif operation == "parse_list":
        expected = list_oracle(context, _rules(kind, manifest), search_moved=True)
    elif operation == "parse_detail":
        expected = detail_oracle(context, require_signal=True)
    else:
        fail("程序故障操作未登记")
    return _case("incident-program-" + operation.replace("_", "-"), context, expected, role="failure")


def failure_case(kind, binding, identity, evidence, manifest):
    """Only server-captured material is accepted; supplied `expected` is never the oracle."""
    if not isinstance(evidence, dict) or len(json.dumps(evidence, ensure_ascii=False).encode()) > 64000:
        fail("没有有界第一方失败原文")
    if evidence.get("type") == "program_input":
        return program_failure_case(kind, binding, identity, evidence, manifest)
    if evidence.get("type") == "response_list" and kind != "official":
        payload = evidence.get("payload")
        if not isinstance(payload, dict):
            fail("缺少原始岗位响应")
        context = prepare_program_input(kind, binding, identity, operation="parse_list", page_url=binding["entry_url"], payload=payload)
        expected = list_oracle(context, _rules(kind, manifest), search_moved=True)
        return _case("incident-list", context, expected, role="failure")
    if evidence.get("type") == "detail_dom":
        html = evidence.get("html")
        if not isinstance(html, str):
            fail("缺少原职责DOM")
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
        if not 30 <= len(text) <= 6000 or not re.search(r"岗位职责|职位描述|工作职责|任职要求|职位要求", text):
            fail("职责资料缺少足够独立语义信号")
        if evidence.get("expected_text_hash") and hashlib.sha256(text.encode()).hexdigest() != evidence["expected_text_hash"]:
            fail("职责原文与捕获摘要不一致")
        # The synthetic envelope is server-owned. The original client/model `expected`
        # job id, title and body are deliberately not trusted or copied.
        page_url = evidence.get("observed_url") if kind == "official" else binding["entry_url"]
        if not page_url:
            fail("官网职责失败缺少第一方实际观察URL")
        job = JobDocument(source_job_id="evidence-only", title="职责证据（不产生业务岗位）", url=page_url)
        context = prepare_program_input(kind, binding, identity, operation="parse_detail", page_url=page_url, html=html, job=job)
        return _case("incident-detail", context, detail_oracle(context, require_signal=True), role="failure")
    fail("无法独立建立当前故障的字段真值，不让模型编造expected")
