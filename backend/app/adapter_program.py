"""Trusted evidence boundary for remote adapter programs; never imports candidate code."""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Comment
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError, model_validator

from app.collection import CollectionError, JobDocument, jsonld_jobs, require_readable_page, text_value
from app.repair_evidence import redact_text

PROTOCOL = "adapter-program/1"
ROOTS = {"boss": "zhipin.com", "liepin": "liepin.com", "zhaopin": "zhaopin.com", "51job": "51job.com", "official": None}
PRIVATE_KEY = re.compile(r"cookie|token|secret|password|authorization|session|phone|mobile|email|avatar|recruiter|bossName|hrName|contact|account|userInfo|userProfile|resume|wechat|credential|accessKey|securityId|^lid$", re.I)
PRIVATE_REGION = re.compile(r"account|user[-_]?(?:profile|info|name|card)|login[-_]?user|avatar|recruiter|boss[-_]?(?:info|name|card)|hr[-_]?(?:info|name|card)|contact|resume", re.I)
ATTRIBUTES = {"id", "class", "title", "aria-label", "data-job-id", "data-company-id", "data-company-name", "href"}
LIST_FIELDS = {"id", "title", "company", "company_id", "salary", "location", "experience", "education", "department", "function", "url_area"}
MAX_INPUT = 1_000_000
MAX_OUTPUT = 300_000


def invalid(message="代码适配器输出未通过第一方证据验证"):
    return CollectionError("adapter_program_invalid", message)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RowReference(StrictModel):
    document_id: Literal["json", "dom"]
    pointer: StrictStr | None = Field(default=None, max_length=1000)
    selector: StrictStr | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def matching_reference(self):
        if (self.document_id == "json" and (self.pointer is None or self.selector is not None)) or (
            self.document_id == "dom" and (not self.selector or self.pointer is not None)
        ):
            raise ValueError("行证据类型不一致")
        return self


class Evidence(StrictModel):
    pointer: StrictStr | None = Field(default=None, max_length=1000)
    selector: StrictStr | None = Field(default=None, max_length=200)
    attribute: StrictStr | None = Field(default=None, max_length=50)
    start: StrictInt | None = Field(default=None, ge=0, le=100000)
    end: StrictInt | None = Field(default=None, ge=1, le=100000)

    @model_validator(mode="after")
    def one_reference(self):
        if (self.pointer is None) == (self.selector is None):
            raise ValueError("字段必须有一种原文证据")
        if self.pointer is not None and self.attribute is not None:
            raise ValueError("JSON证据不能指定HTML属性")
        if (self.start is None) != (self.end is None) or (self.start is not None and self.start >= self.end):
            raise ValueError("原文字段切片不正确")
        return self


class Claim(StrictModel):
    value: StrictStr = Field(min_length=1, max_length=16000)
    evidence: Evidence


class Record(StrictModel):
    row: RowReference
    fields: dict[str, Claim] = Field(min_length=1, max_length=12)
    locations: list[Claim] = Field(default_factory=list, max_length=20)
    job_link_id: StrictStr | None = Field(default=None, max_length=80)


class Follow(StrictModel):
    type: Literal["follow"]
    link_id: StrictStr = Field(min_length=1, max_length=80)


class Scroll(StrictModel):
    type: Literal["scroll"]
    pixels: StrictInt = Field(ge=1, le=1500)


class Wait(StrictModel):
    type: Literal["wait"]
    milliseconds: StrictInt = Field(ge=1, le=1500)


class Output(StrictModel):
    protocol: Literal["adapter-program/1"]
    input_id: StrictStr
    operation: Literal["parse_list", "parse_detail", "navigation"]
    records: list[Record] = Field(default_factory=list, max_length=100)
    row: RowReference | None = None
    fields: dict[str, Claim] = Field(default_factory=dict, max_length=16)
    navigation: list[Follow | Scroll | Wait] = Field(default_factory=list, max_length=6)


@dataclass(frozen=True)
class ProgramContext:
    _request: dict = field(repr=False)
    _binding: dict = field(repr=False)
    _identity: dict = field(repr=False)
    _links: dict = field(repr=False)
    _tokens: dict = field(repr=False)
    _job: JobDocument | None = field(default=None, repr=False)
    _page_url: str = field(default="", repr=False)
    _official_ids: dict = field(default_factory=dict, repr=False)

    @property
    def request(self):
        """Only this detached JSON object may be passed to an untrusted remote program."""
        return deepcopy(self._request)


@dataclass(frozen=True)
class ProgramNavigation:
    type: str
    url: str | None = field(default=None, repr=False)
    pixels: int | None = None
    milliseconds: int | None = None


@dataclass
class ValidatedProgramList:
    records: list[JobDocument]
    navigation: list[ProgramNavigation]
    raw_count: int
    rejected_company_count: int
    job_navigations: dict[str, str] = field(repr=False)
    identity_bases: dict[str, str] = field(default_factory=dict)

    @property
    def recognized(self):
        return bool(self.records)

    @property
    def jobs(self):
        return [(doc, self.job_navigations[doc.source_job_id]) for doc in self.records]


def _json_size(value, limit):
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError, RecursionError) as exc:
        raise invalid("适配器输入输出不是有界JSON") from exc
    if size > limit:
        raise invalid("适配器输入输出超过大小预算")


def _public_url(url, kind, binding=None):
    parsed = urlparse(url)
    if kind == "official":
        if not binding or binding.get("kind") != "careers":
            return None
        entry = urlparse(binding.get("entry_url", ""))
        try:
            if (entry.scheme not in {"http", "https"} or not entry.hostname or entry.username or entry.password
                    or parsed.scheme != entry.scheme or parsed.hostname != entry.hostname
                    or (parsed.port or (443 if parsed.scheme == "https" else 80)) != (entry.port or (443 if entry.scheme == "https" else 80))
                    or parsed.username or parsed.password or len(url) > 3000 or re.search(r"[\x00-\x20\\]", url)):
                return None
        except ValueError:
            return None
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port and parsed.port != (443 if parsed.scheme == "https" else 80):
            host += ":" + str(parsed.port)
        return urlunparse(parsed._replace(netloc=host, path=parsed.path or "/", fragment=""))
    root = ROOTS[kind]
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or (parsed.hostname != root and not parsed.hostname.endswith("." + root))):
        return None
    try:
        if parsed.port not in {None, 443}:
            return None
    except ValueError:
        return None
    if len(url) > 3000 or re.search(r"[\x00-\x20\\]", url):
        return None
    return urlunparse(parsed._replace(scheme="https", netloc=parsed.hostname, path=parsed.path or "/", fragment=""))


def _link_job_id(url, kind):
    if kind == "official":
        return None
    patterns = {"boss": r"/job_detail/([A-Za-z0-9_~-]+)\.html",
                "liepin": r"/job/([A-Za-z0-9_~-]+)\.shtml",
                "zhaopin": r"/jobdetail/([A-Za-z0-9_~-]+)\.htm",
                "51job": r"/(?!all/)[A-Za-z0-9-]+/(\d+)\.html"}
    match = re.fullmatch(patterns[kind], urlparse(url).path)
    return match.group(1) if match else None


def _safe_text(value):
    value = redact_text(value)
    return re.sub(r"(?:cookie|token|password|secret|authorization)\s*[:=]\s*[^\s;，；]+", "[秘密已移除]", value, flags=re.I)


def _link(links, destination, purpose, label, job_id=None):
    identifier = "link_" + hashlib.sha256((purpose + "\n" + destination).encode()).hexdigest()
    if identifier not in links and len(links) >= 200:
        return None
    links[identifier] = {"url": destination, "purpose": purpose, "label": _safe_text(str(label))[:200]}
    if job_id:
        links[identifier]["job_id"] = job_id
    return identifier


def _official_url(url):
    parsed = urlparse(url)
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and not PRIVATE_KEY.search(re.sub(r"[_-]", "", key))]
    return urlunparse(parsed._replace(path=parsed.path.rstrip("/") or "/", query=urlencode(pairs), fragment=""))


def _official_plan(binding):
    config = binding.get("config") or {}
    plan = config.get("website_plan")
    if plan is None:
        return None
    if not isinstance(plan, dict) or hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest() != config.get("plan_hash"):
        raise invalid("官网CSS计划缺少第一方保存的匹配指纹")
    from app.website_agent import WebsitePlan
    try:
        return WebsitePlan.model_validate(plan).model_dump()
    except ValidationError as exc:
        raise invalid("已保存官网CSS计划不符合只读采集契约") from exc


def _official_jsonld(html, payload, page_url, binding, links, identifiers, *, detail=False):
    blocks = []
    if html is not None:
        for script in BeautifulSoup(html, "html.parser").select('script[type="application/ld+json"]')[:30]:
            try:
                blocks.append(json.loads(script.string or script.get_text()))
            except (ValueError, TypeError):
                blocks.append(None)
    elif payload is not None:
        blocks = [deepcopy(payload)]
    visits = [0]
    def walk(value, depth=0):
        visits[0] += 1
        if depth > 20 or visits[0] > 20000:
            raise invalid("JSON-LD超过节点或层级预算")
        if isinstance(value, list):
            for child in value:
                walk(child, depth + 1)
        elif isinstance(value, dict):
            for key in list(value):
                if key.startswith("_program_"):
                    del value[key]
            types = value.get("@type", [])
            if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
                raw_url = value.get("url") or (page_url if detail else None)
                destination = _public_url(urljoin(page_url, raw_url), "official", binding) if isinstance(raw_url, str) else None
                if destination:
                    identifier = _link(links, destination, "job", value.get("title", ""))
                    if identifier:
                        value["_program_job_link_id"] = identifier
                        # Preserve native jsonld_jobs identity exactly across the code transition.
                        identifiers[identifier] = (text_value(value.get("identifier")) or urljoin(page_url, raw_url))[:500]
                for key in ("title",):
                    if isinstance(value.get(key), str):
                        value[key] = BeautifulSoup(value[key], "html.parser").get_text(" ", strip=True)
            for key, child in list(value.items()):
                if not key.startswith("_program_") and isinstance(child, (dict, list)):
                    walk(child, depth + 1)
    walk(blocks)
    return {"blocks": blocks} if blocks else None


def _tencent_payload(payload, operation, page_url, binding, links, job):
    parsed = urlparse(page_url)
    if (urlparse(binding["entry_url"]).hostname != "careers.tencent.com" or parsed.hostname != "careers.tencent.com"
            or parsed.scheme != "https" or operation not in {"parse_list", "parse_detail", "navigation"}):
        raise invalid("腾讯程序仅使用登记官方门户的固定读取接口")
    value = deepcopy(payload)
    if not isinstance(value, dict):
        raise invalid("腾讯原始响应必须为JSON对象")
    if operation in {"parse_list", "navigation"}:
        if (parsed.path != "/tencentcareer/api/post/Query" or type(value.get("Code")) is not int or value["Code"] != 200
                or not isinstance(value.get("Data"), dict) or not isinstance(value["Data"].get("Posts"), list)):
            raise CollectionError("source_contract_changed", "腾讯列表响应状态或原Data结构未验证")
        rows = value["Data"]["Posts"]
        query = dict(parse_qsl(parsed.query))
        if (type(value["Data"].get("Count")) is int and rows and query.get("pageIndex", "").isdigit()
                and query.get("pageSize", "").isdigit() and 0 < int(query["pageIndex"]) < 1000000
                and 0 < int(query["pageSize"]) <= 100 and value["Data"]["Count"] > int(query["pageIndex"]) * int(query["pageSize"])):
            next_query = {"pageIndex": int(query["pageIndex"]) + 1, "pageSize": int(query["pageSize"]),
                          "language": "zh-cn", "area": "cn"}
            _link(links, "https://careers.tencent.com/tencentcareer/api/post/Query?" + urlencode(next_query), "next", "已登记腾讯API下一页")
    elif operation == "parse_detail":
        requested = dict(parse_qsl(parsed.query)).get("postId")
        if parsed.path != "/tencentcareer/api/post/ByPostId" or not job or requested != job.source_job_id:
            raise CollectionError("identity_review", "腾讯详情请求未绑定已核验PostId")
        if "verified_details" in value:
            rows = value["verified_details"]
        else:
            if type(value.get("Code")) is not int or value["Code"] != 200 or not isinstance(value.get("Data"), dict):
                raise CollectionError("source_contract_changed", "腾讯详情原始响应未成功")
            rows = [value["Data"]]
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or str(rows[0].get("PostId", "")) != requested:
            raise CollectionError("identity_review", "腾讯详情响应PostId与请求不一致")
    for row in rows:
        if not isinstance(row, dict):
            continue
        row.pop("_program_job_link_id", None)
        identifier = str(row.get("PostId", ""))
        if re.fullmatch(r"[0-9]{1,30}", identifier):
            ref = _link(links, "https://careers.tencent.com/jobdesc.html?postId=" + identifier, "job", row.get("RecruitPostName", ""), identifier)
            if ref:
                row["_program_job_link_id"] = ref
    return value


def _sanitize_json(payload, tokens):
    visited = [0]
    def walk(value, pointer="", depth=0):
        visited[0] += 1
        if depth > 20 or visited[0] > 20000:
            raise invalid("JSON资料超过层级或节点预算")
        if isinstance(value, dict):
            private_navigation = {key: str(value[key]) for key in ("securityId", "lid")
                                  if isinstance(value.get(key), (str, int)) and not isinstance(value.get(key), bool)
                                  and len(str(value[key])) <= 1000}
            if private_navigation:
                tokens[pointer] = private_navigation
            return {key: walk(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1)
                    for key, item in value.items() if isinstance(key, str) and len(key) <= 150
                    and not PRIVATE_KEY.search(re.sub(r"[_-]", "", key))}
        if isinstance(value, list):
            # Do not filter/compact arrays: RFC6901 indexes must still name the same row.
            return [walk(item, pointer + "/" + str(index), depth + 1) for index, item in enumerate(value)]
        if isinstance(value, str):
            if len(value) > 50000:
                raise invalid("单个资料字段超过大小预算")
            return _safe_text(value)
        if value is None or type(value) in {int, float, bool}:
            return value
        raise invalid("资料包含非JSON类型")
    return walk(payload)


def _sanitize_dom(html, page_url, kind, links, *, binding=None, plan=None):
    require_readable_page(html)
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for node in list(soup.find_all(True)):
        if node.parent is None:
            continue
        node.attrs.pop("data-program-link-id", None)
        marker = " ".join([str(node.get("id", "")), *node.get("class", [])])
        if (node.name in {"script", "style", "form", "input", "textarea", "iframe", "svg", "header", "footer", "aside", "button", "object", "embed"}
                or PRIVATE_REGION.search(marker)
                or (node.name == "nav" and not re.search(r"pager|pagination", marker, re.I))):
            node.decompose()
    if len(soup.find_all(True)) > 10000:
        raise invalid("HTML资料超过节点预算")
    approved_nodes, approved_next = set(), set()
    if kind == "official" and plan:
        approved_nodes = {id(node) for node in soup.select(plan["job_link_selector"], limit=200) if node.name == "a"}
        if plan.get("next_selector"):
            approved_next = {id(node) for node in soup.select(plan["next_selector"], limit=10) if node.name == "a"}
    for anchor in soup.select("a[href]"):
        destination = _public_url(urljoin(page_url, anchor.get("href", "")), kind, binding)
        purpose = "job" if destination and (_link_job_id(destination, kind) or id(anchor) in approved_nodes) else None
        label = anchor.get_text(" ", strip=True)
        if destination and id(anchor) in approved_next and destination != page_url:
            target = urlparse(destination)
            if not re.search(r"/(?:login|logout|account|apply|delete|remove|submit|admin)(?:/|$)", target.path, re.I) and not any(
                key.lower() in {"action", "delete", "remove", "submit", "apply"} for key, _ in parse_qsl(target.query)
            ):
                purpose = "next"
        if destination and not purpose and ("next" in anchor.get("rel", []) or re.fullmatch(r"下一页|下页|Next|next|[>›»]", label)):
            target, current = urlparse(destination), urlparse(page_url)
            params = parse_qsl(target.query, keep_blank_values=True)
            if (target.hostname == current.hostname and target.path == current.path and params
                    and target.query != current.query and all(key in {"page", "pageIndex", "pageNum", "pn", "p", "offset", "start"}
                    and value.isdigit() and 0 <= int(value) <= 1000000 for key, value in params)):
                purpose = "next"
        if purpose and len(links) < 200:
            link_id = _link(links, destination, purpose, label, _link_job_id(destination, kind) if purpose == "job" else None)
            anchor["data-program-link-id"] = link_id
            anchor["href"] = urlunparse(urlparse(destination)._replace(query="", fragment=""))
        else:
            anchor.attrs.pop("href", None)
    for node in soup.find_all(True):
        attrs = {}
        for key, value in node.attrs.items():
            if key not in ATTRIBUTES | {"data-program-link-id"}:
                continue
            if key == "href":
                attrs[key] = value  # Already a canonical, observed public job/next link.
            else:
                values = value if isinstance(value, list) else [value]
                values = [_safe_text(str(item))[:300] for item in values]
                attrs[key] = values if key == "class" else values[0]
        node.attrs = attrs
    for text in list(soup.find_all(string=True)):
        text.replace_with(_safe_text(str(text)))
    return str(soup)


def prepare_program_input(kind, binding, identity, *, operation, page_url, payload=None, html=None, job=None):
    if kind not in ROOTS or operation not in {"parse_list", "parse_detail", "navigation"}:
        raise invalid("代码适配器平台或操作未登记")
    if not _public_url(page_url, kind, binding):
        raise invalid("资料页面不属于当前招聘平台")
    if payload is None and html is None:
        raise invalid("没有第一方取得的资料")
    if operation == "parse_detail" and not isinstance(job, JobDocument):
        raise invalid("详情解析必须携带已核验的岗位")
    _json_size({"payload": payload, "html": html}, MAX_INPUT)
    documents, links, tokens, official_ids = [], {}, {}, {}
    plan = _official_plan(binding) if kind == "official" else None
    tencent = kind == "official" and binding.get("adapter") == "tencent"
    if tencent:
        payload = _tencent_payload(payload, operation, page_url, binding, links, job)
    elif kind == "official":
        payload = _official_jsonld(html, payload, page_url, binding, links, official_ids, detail=operation == "parse_detail")
        if operation == "parse_detail" and job and _official_url(job.url) == _official_url(page_url):
            _link(links, page_url, "job", job.title)
    if payload is not None:
        documents.append({"id": "json", "type": "json", "content": _sanitize_json(payload, tokens)})
    if html is not None:
        documents.append({"id": "dom", "type": "html", "content": _sanitize_dom(html, page_url, kind, links, binding=binding, plan=plan)})
    request = {"protocol": PROTOCOL, "operation": operation, "kind": kind,
               "documents": documents, "links": [{"id": key, "purpose": value["purpose"], "label": value["label"],
                                                    **({"job_id": value["job_id"]} if value.get("job_id") else {})}
                                                  for key, value in links.items()]}
    if job:
        public_id = ("job_" + hashlib.sha256(job.source_job_id.encode()).hexdigest()
                     if kind == "official" and re.match(r"https?://", job.source_job_id) else job.source_job_id)
        request["job"] = {"id": public_id, "title": _safe_text(job.title)}
    if plan:
        request["plan"] = plan
    if tencent:
        request["board"] = "tencent"
    if kind == "official" and operation == "parse_detail" and job:
        request["current_job_link_id"] = next((key for key, value in links.items()
                                                if _official_url(value["url"]) == _official_url(job.url)), None)
    request["input_id"] = "input_" + hashlib.sha256(json.dumps(
        request, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    _json_size(request, MAX_INPUT)
    return ProgramContext(request, deepcopy(binding), deepcopy(identity), links, tokens, job.model_copy(deep=True) if job else None, page_url, official_ids)


def capture_program_failure(context):
    """Internal first-party capsule. Never accept this shape as a public client oracle."""
    request = context.request
    binding = {"kind": context._binding.get("kind", request["kind"]),
               "company_id": str(context._binding["company_id"]) if context._binding.get("company_id") else None,
               "entry_url": _official_url(context._binding["entry_url"]),
               "external_company_id": str(context._binding["external_company_id"]) if context._binding.get("external_company_id") is not None else None}
    capsule = {"type": "program_input", "kind": request["kind"], "operation": request["operation"],
               "request": request, "request_sha256": hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
               "observed_url": _official_url(context._page_url), "binding": binding,
               "captured_links": [{"id": key, **value, "url": _official_url(value["url"])} for key, value in context._links.items()]}
    body = json.dumps(capsule, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(body) > 64000:
        return None
    capsule["capture_sha256"] = hashlib.sha256(body).hexdigest()
    return capsule


def rebind_official_detail_job(binding, identity, job, *, requested_url, final_url, html, id_basis):
    """First-party native identity continuity after an already-validated same-origin fetch."""
    if id_basis not in {"css", "jsonld"} or not _public_url(requested_url, "official", binding) or not _public_url(final_url, "official", binding):
        raise invalid("官网详情的来源或原岗位身份依据不匹配")
    if _official_url(job.url) != _official_url(requested_url):
        raise invalid("官网详情请求不属于原岗位")
    require_readable_page(html)
    structured = jsonld_jobs(html, final_url)
    if structured:
        doc = structured[0]
        if not _public_url(doc.url, "official", binding) or _official_url(doc.url) not in {_official_url(requested_url), _official_url(final_url)}:
            raise invalid("详情JSON-LD指向其他岗位或来源")
        names = [identity.get("name"), identity.get("official_name"), *identity.get("aliases", [])]
        if doc.recruiting_entity and re.sub(r"\s+", "", doc.recruiting_entity).casefold() not in {
            re.sub(r"\s+", "", name).casefold() for name in names if isinstance(name, str) and name
        }:
            raise CollectionError("identity_review", "官网详情招聘主体与绑定公司不一致")
        doc.observed_at = job.observed_at
        doc.description = ""  # The actual code program must still produce and prove its body.
        return doc
    doc = job.model_copy(deep=True)
    doc.url = final_url
    if id_basis == "css":
        doc.source_job_id = final_url[:500]
    return doc


def _pointer(root, pointer):
    if pointer == "":
        return root
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise invalid("JSON pointer格式不正确")
    tokens = pointer[1:].split("/")
    if len(tokens) > 20:
        raise invalid("JSON pointer层级过深")
    try:
        for token in tokens:
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(root, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", token):
                    raise invalid("JSON数组索引不正确")
                root = root[int(token)]
            elif isinstance(root, dict):
                root = root[token]
            else:
                raise invalid("JSON pointer没有指向原始字段")
    except (KeyError, IndexError) as exc:
        raise invalid("JSON pointer没有对应资料") from exc
    return root


def _select(root, selector, *, row=False):
    if selector == "" and not row:
        return root
    if (not selector or re.search(r":(?:has|contains|is|where|matches)\b|[,*]|(?<![-\w.#])(?:html|body)(?![-\w])", selector, re.I)):
        raise invalid("HTML证据选择器过宽或过于复杂")
    try:
        nodes = root.select(selector, limit=2)
    except Exception as exc:
        raise invalid("HTML证据选择器无效") from exc
    if len(nodes) != 1:
        raise invalid("HTML证据必须唯一选中岗位区域")
    if nodes[0] is not root and root not in nodes[0].parents:
        raise invalid("HTML证据越过岗位区域")
    return nodes[0]


def _row(context, reference):
    document = next((doc for doc in context._request["documents"] if doc["id"] == reference.document_id), None)
    if document is None:
        raise invalid("候选引用了不存在的资料")
    if reference.document_id == "json":
        row = _pointer(document["content"], reference.pointer)
        if not isinstance(row, dict):
            raise invalid("岗位行证据必须是JSON对象")
        if context._request.get("board") == "tencent":
            if not row.get("_program_job_link_id"):
                raise invalid("腾讯行没有第一方PostId链接证明")
        elif context._request["kind"] == "official":
            types = row.get("@type", [])
            if not (types == "JobPosting" or isinstance(types, list) and "JobPosting" in types) or not row.get("_program_job_link_id"):
                raise invalid("官网JSON证据必须定位到第一方已核验URL的JobPosting")
        elif context._request["operation"] == "parse_list":
            parent_path, _, index = reference.pointer.rpartition("/")
            if not re.fullmatch(r"0|[1-9][0-9]*", index) or not isinstance(_pointer(document["content"], parent_path), list):
                raise invalid("列表岗位证据必须定位到原JSON数组中的单个对象")
        return row
    row = _select(BeautifulSoup(document["content"], "html.parser"), reference.selector, row=True)
    if context._request["operation"] == "parse_list":
        if context._request["kind"] == "official":
            refs = {node.get("data-program-link-id") for node in [row, *row.select("[data-program-link-id]")]
                    if node.get("data-program-link-id") and context._links.get(node.get("data-program-link-id"), {}).get("purpose") == "job"}
            if len(refs) != 1:
                raise invalid("官网岗位行必须对应一个已观察岗位链接")
            return row
        identifiers = {_link_job_id(node.get("href", ""), context._request["kind"])
                       for node in row.select("a[href]")}
        identifiers.discard(None)
        identifiers.update(node.get("data-job-id") for node in row.select("[data-job-id]") if node.get("data-job-id"))
        if row.get("data-job-id"):
            identifiers.add(row["data-job-id"])
        if len(identifiers) > 1:
            raise invalid("HTML岗位区域混入多个岗位，不能跨岗位拼接身份")
    return row


def _claim(row, claim, document_id):
    evidence = claim.evidence
    if document_id == "json":
        if evidence.pointer is None:
            raise invalid("JSON岗位不能引用其他HTML区域")
        value = _pointer(row, evidence.pointer)
        if type(value) not in {str, int, float}:
            raise invalid("字段证据不是公开标量")
        value = str(value)
    else:
        if evidence.selector is None:
            raise invalid("HTML岗位不能引用其他JSON区域")
        node = _select(row, evidence.selector)
        if evidence.attribute is not None:
            if evidence.attribute not in ATTRIBUTES - {"class"}:
                raise invalid("字段引用了未允许属性")
            value = node.get(evidence.attribute)
            if not isinstance(value, str):
                raise invalid("字段属性没有对应原文")
        else:
            value = node.get_text(" ", strip=True)
    if evidence.start is not None:
        if evidence.end > len(value):
            raise invalid("字段切片超出原文")
        value = value[evidence.start:evidence.end]
    def normalize(text):
        return re.sub(r"\s+", " ", text).strip()
    if not normalize(value) or normalize(value) != normalize(claim.value):
        raise invalid("候选字段与原文证据不一致")
    return normalize(value)


def _output(context, output, operation):
    _json_size(output, MAX_OUTPUT)
    try:
        result = Output.model_validate(output)
    except ValidationError as exc:
        raise invalid() from exc
    if (result.input_id != context._request["input_id"] or result.operation != operation
            or context._request["operation"] != operation):
        raise invalid("候选输出不属于本次资料与操作")
    if operation == "parse_list" and (result.row is not None or result.fields):
        raise invalid()
    detail_fields = {"description", "requirements"}
    if context._request.get("board") == "tencent":
        detail_fields |= {"title", "company", "location", "department", "function", "experience", "source_updated_at"}
    if operation == "parse_detail" and (result.records or result.row is None or set(result.fields) - detail_fields or "description" not in result.fields):
        raise invalid()
    if operation == "navigation" and (result.records or result.row is not None or result.fields):
        raise invalid()
    return result


def _navigation(context, actions, allowed_job_ids=(), allowed_job_urls=()):
    result, followed = [], set()
    if sum(action.milliseconds for action in actions if isinstance(action, Wait)) > 3000:
        raise invalid("等待计划超过预算")
    if sum(action.pixels for action in actions if isinstance(action, Scroll)) > 3000:
        raise invalid("滚动计划超过预算")
    for action in actions:
        if isinstance(action, Follow):
            link = context._links.get(action.link_id)
            if not link or action.link_id in followed:
                raise invalid("导航链接未被观察或重复")
            if link["purpose"] == "job":
                allowed = (_official_url(link["url"]) in allowed_job_urls if context._request["kind"] == "official"
                           else _link_job_id(link["url"], context._request["kind"]) in allowed_job_ids)
                if not allowed:
                    raise invalid("导航岗位未通过本公司身份核验")
            followed.add(action.link_id)
            result.append(ProgramNavigation("follow", url=link["url"]))
        elif isinstance(action, Scroll):
            result.append(ProgramNavigation("scroll", pixels=action.pixels))
        else:
            result.append(ProgramNavigation("wait", milliseconds=action.milliseconds))
    return result


def validate_navigation(context, output):
    """Returned URLs still require the collector's normal DNS/redirect/page-budget checks."""
    allowed = {context._job.source_job_id} if context._job else set()
    urls = {_official_url(context._job.url)} if context._job else set()
    return _navigation(context, _output(context, output, "navigation").navigation, allowed, urls)


def _official_record(context, reference, row, values, link_id):
    link = context._links.get(link_id)
    if not link or link["purpose"] != "job":
        raise invalid("官网岗位没有第一方观察到的同域链接")
    if context._request.get("board") == "tencent":
        identifier = str(row.get("PostId", ""))
        if (reference.document_id != "json" or not re.fullmatch(r"[0-9]{1,30}", identifier)
                or row.get("_program_job_link_id") != link_id or values.get("id") != identifier
                or values.get("title") != re.sub(r"\s+", " ", str(row.get("RecruitPostName", ""))).strip()
                or link["url"] != "https://careers.tencent.com/jobdesc.html?postId=" + identifier):
            raise invalid("腾讯岗位编号、标题或固定链接与原行证据不一致")
        _tencent_fields(row, values)
        return identifier, link["url"], link["url"], row.get("ComName") if isinstance(row.get("ComName"), str) else None
    if reference.document_id == "json":
        if row.get("_program_job_link_id") != link_id or values.get("title") != re.sub(r"\s+", " ", str(row.get("title", ""))).strip():
            raise invalid("官网JSON标题/链接不属于同一原JobPosting")
        organization = row.get("hiringOrganization")
        organization = organization.get("name") if isinstance(organization, dict) else organization
    else:
        refs = {node.get("data-program-link-id") for node in [row, *row.select("[data-program-link-id]")]}
        if link_id not in refs:
            raise invalid("官网岗位链接不在当前原文岗位行")
        organization = values.get("company")
    organization = organization.strip() if isinstance(organization, str) else None
    names = [context._identity.get("name"), context._identity.get("official_name"), *context._identity.get("aliases", [])]
    normalized = {re.sub(r"\s+", "", name).casefold() for name in names if isinstance(name, str) and name}
    if organization and re.sub(r"\s+", "", organization).casefold() not in normalized:
        return None
    if values.get("company") and organization and values["company"] != organization:
        raise invalid("官网公司字段与hiringOrganization不一致")
    url = _official_url(link["url"])
    if reference.document_id == "json":
        identifier = context._official_ids.get(link_id)
        if identifier is None:
            identifier = text_value(row.get("identifier"))
            if not identifier or "已移除" in identifier:
                identifier = link["url"]
            identifier = identifier[:500]
    else:
        # The established CSS collector uses the observed detail URL, not data-job-id.
        identifier = link["url"][:500]
    if values.get("id") and values["id"] != identifier:
        raise invalid("官网岗位编号必须来自原标识或第一方URL摘要")
    return identifier, url, link["url"], organization


def _tencent_fields(row, values):
    mapping = {"title": "RecruitPostName", "company": "ComName", "location": "LocationName", "department": "BGName",
               "function": "CategoryName", "experience": "RequireWorkYearsName", "source_updated_at": "LastUpdateTime",
               "description": "Responsibility", "requirements": "Requirement"}
    for key, source in mapping.items():
        if key in values and values[key] != re.sub(r"\s+", " ", str(row.get(source, ""))).strip():
            raise invalid("腾讯字段必须对应登记原Data字段，不能借其他值伪造语义")


def validate_list_output(context, output):
    from app.platform_collectors import same_company  # Avoid importing the caller during startup.

    result = _output(context, output, "parse_list")
    kind = context._request["kind"]
    docs, navigations, seen, rejected, bases = [], {}, set(), 0, {}
    for record in result.records:
        required = {"title"} if kind == "official" else {"id", "title"}
        if set(record.fields) - LIST_FIELDS or not required.issubset(record.fields):
            raise invalid("岗位字段不完整或包含未允许字段")
        if any(record.fields[key].evidence.start is not None for key in ("company", "company_id") if key in record.fields):
            raise invalid("公司身份必须核对完整原字段，不能截取名称或编号冒充其他主体")
        row = _row(context, record.row)
        values = {key: _claim(row, claim, record.row.document_id) for key, claim in record.fields.items()}
        if any(len(value) > (500 if key == "title" else 250) for key, value in values.items()):
            raise invalid("岗位字段超过存储预算")
        if kind == "official":
            found = _official_record(context, record.row, row, values, record.job_link_id)
            if found is None:
                rejected += 1
                continue
            identifier, url, navigation, organization = found
            if identifier in seen:
                raise invalid("候选重复输出同一官网岗位")
            seen.add(identifier)
            bases[identifier] = "jsonld" if record.row.document_id == "json" else "css"
            locations = [_claim(row, claim, record.row.document_id) for claim in record.locations]
            if not locations and values.get("location"):
                locations = [values["location"]]
            docs.append(JobDocument(source_job_id=identifier, title=values["title"], url=url,
                                    recruiting_entity=organization, locations=locations,
                                    salary_raw=values.get("salary"), experience=values.get("experience"), education=values.get("education"),
                                    department=values.get("department"), function=values.get("function")))
            navigations[identifier] = navigation
            continue
        identifier = values["id"]
        if not re.fullmatch(r"[A-Za-z0-9_~-]{1,180}", identifier) or len(values["title"]) > 500:
            raise invalid("岗位编号或标题不正确")
        if not same_company(values.get("company"), values.get("company_id"), kind, context._binding, context._identity):
            rejected += 1
            continue
        if identifier in seen:
            raise invalid("候选重复输出同一岗位")
        seen.add(identifier)
        observed_link = context._links.get(record.job_link_id) if record.job_link_id else None
        if record.job_link_id and (not observed_link or observed_link["purpose"] != "job"
                                   or _link_job_id(observed_link["url"], kind) != identifier):
            raise invalid("岗位链接没有对应已核验编号")
        if kind == "51job":
            area = values.get("url_area")
            if not area and observed_link:
                area = urlparse(observed_link["url"]).path.split("/")[1]
            if not area or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", area):
                raise invalid("岗位缺少有证据的URL地区字段")
            url = f"https://jobs.51job.com/{area}/{identifier}.html"
        else:
            url = {"boss": "https://www.zhipin.com/job_detail/{}.html", "liepin": "https://www.liepin.com/job/{}.shtml",
                   "zhaopin": "https://www.zhaopin.com/jobdetail/{}.htm"}[kind].format(identifier)
        navigation = url
        if observed_link:
            navigation = observed_link["url"]
        elif kind == "boss" and record.row.document_id == "json" and context._tokens.get(record.row.pointer):
            navigation += "?" + urlencode(context._tokens[record.row.pointer])
        locations = [_claim(row, claim, record.row.document_id) for claim in record.locations]
        if any(len(value) > 200 for value in locations):
            raise invalid("岗位城市字段过长")
        if not locations and values.get("location"):
            locations = [values["location"]]
        salary = values.get("salary")
        if salary and re.search(r"[\ue000-\uf8ff*]", salary):
            salary = None
        docs.append(JobDocument(source_job_id=identifier, title=values["title"], url=url,
                                recruiting_entity=values.get("company"), locations=locations, salary_raw=salary,
                                experience=values.get("experience"), education=values.get("education"),
                                department=values.get("department"), function=values.get("function")))
        navigations[identifier] = navigation
    return ValidatedProgramList(docs, _navigation(context, result.navigation, seen,
                                                  {_official_url(url) for url in navigations.values()}),
                                len(result.records), rejected, navigations, bases)


def validate_detail_output(context, output):
    result = _output(context, output, "parse_detail")
    row = _row(context, result.row)
    values = {key: _claim(row, claim, result.row.document_id) for key, claim in result.fields.items()}
    if context._job is None:
        raise invalid("缺少第一方已核验岗位")
    doc = context._job.model_copy(deep=True)
    if context._request["kind"] == "official":
        if context._request.get("board") == "tencent":
            if str(row.get("PostId", "")) != doc.source_job_id or dict(parse_qsl(urlparse(context._page_url).query)).get("postId") != doc.source_job_id:
                raise invalid("腾讯详情原行与已核验岗位不一致")
            _tencent_fields(row, values)
            for key, target in {"title": "title", "company": "recruiting_entity", "department": "department", "function": "function",
                                "experience": "experience", "source_updated_at": "source_updated_at"}.items():
                if key in values:
                    setattr(doc, target, values[key])
            if "company" not in values and row.get("ComName") == "":
                doc.recruiting_entity = ""
            if values.get("location"):
                doc.locations = [values["location"]]
        elif _official_url(context._page_url) != _official_url(doc.url):
            raise invalid("官网详情不属于本次已核验岗位URL")
        if result.row.document_id == "json" and context._request.get("board") != "tencent":
            link = context._links.get(row.get("_program_job_link_id"))
            if not link or _official_url(link["url"]) != _official_url(doc.url):
                raise invalid("官网JSON详情指向其他岗位")
            org = row.get("hiringOrganization")
            org = org.get("name") if isinstance(org, dict) else org
            names = [context._identity.get("name"), context._identity.get("official_name"), *context._identity.get("aliases", [])]
            if isinstance(org, str) and org.strip() and re.sub(r"\s+", "", org).casefold() not in {
                re.sub(r"\s+", "", name).casefold() for name in names if isinstance(name, str) and name
            }:
                raise CollectionError("identity_review", "官网JobPosting招聘主体与绑定公司不一致")
    doc.description, doc.requirements = values["description"], values.get("requirements", doc.requirements)
    if result.row.document_id == "json":
        doc.description, doc.requirements = text_value(doc.description), text_value(doc.requirements)
    return doc, _navigation(context, result.navigation, {doc.source_job_id}, {_official_url(doc.url)})
