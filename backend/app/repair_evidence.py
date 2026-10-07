"""Capture bounded job-only evidence; authenticated pages/responses never leave this module."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlparse, urlunparse

from bs4 import BeautifulSoup, Comment

from app.collection import CollectionError, require_readable_page

REPAIRABLE_ERRORS = {"source_contract_changed", "selector_mismatch"}
_capture = ContextVar("collection_repair_capture", default=None)
PRIVATE_KEY = re.compile(r"cookie|token|secret|password|security|authorization|phone|mobile|email|avatar|staff|recruiter|boss|hr|wechat|session", re.I)


def clean_url(url):
    value = urlparse(url)
    if value.scheme not in {"http", "https"} or not value.hostname or value.username or value.password:
        return ""
    return urlunparse(value._replace(query="", fragment=""))


def redact_text(value):
    value = re.sub(r"https?://\S+", "[链接已移除]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已移除]", value)
    value = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[电话已移除]", value)
    value = re.sub(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)", "[电话已移除]", value)
    value = re.sub(r"(?<!\d)\d{17}[\dXx](?!\d)", "[身份号码已移除]", value)
    value = re.sub(r"(?:Bearer\s+[A-Za-z0-9._~-]+|sk-[A-Za-z0-9_-]{12,})", "[密钥已移除]", value, flags=re.I)
    value = re.sub(r"(?:联系人|联系HR|招聘负责人|微信|QQ)[：: ]*[^。；\n]{0,80}", "[联系人已移除]", value, flags=re.I)
    return value


def compact_binding(binding, identity):
    return {"entry_url": clean_url(binding["entry_url"]),
            "external_company_id": binding.get("external_company_id")}, {
                "name": identity.get("name"), "official_name": identity.get("official_name"),
                "aliases": identity.get("aliases", [])[:10],
            }


@contextmanager
def capture_repairs(kind, binding, identity):
    state = {"kind": kind, "binding": binding, "identity": identity, "evidence": None}
    token = _capture.set(state)
    try:
        yield state
    finally:
        _capture.reset(token)


def capture_detail(html, doc):
    state = _capture.get()
    if state is None or (state["evidence"] and state["evidence"].get("type") == "detail_dom"):
        return
    state["evidence"] = None
    try:
        require_readable_page(html)
    except CollectionError:
        return
    soup = BeautifulSoup(html, "html.parser")
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()
    for node in soup.select("script,style,form,input,textarea,iframe,svg,nav,header,footer,aside,button"):
        node.decompose()
    options = []
    for node in soup.select("div[class],section[class],article[class],div[id],section[id],article[id]")[:200]:
        text = node.get_text(" ", strip=True)
        if 40 <= len(text) <= 6000 and re.search(r"岗位职责|职位描述|工作职责|任职要求|职位要求", text):
            options.append((len(text), node))
    if not options:
        return
    node = min(options, key=lambda item: item[0])[1]
    evidence_dom = BeautifulSoup(str(node), "html.parser")
    for element in evidence_dom.find_all(True):
        attrs = {}
        for key in ("class", "id"):
            values = element.get(key, [])
            values = values if isinstance(values, list) else [values]
            values = [value for value in values if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,79}", value)
                      and not re.search(r"[A-Fa-f0-9]{24,}", value)]
            if values:
                attrs[key] = values if key == "class" else values[0]
        element.attrs = attrs
    for text in list(evidence_dom.find_all(string=True)):
        text.replace_with(redact_text(str(text)))
    html = str(evidence_dom)
    text = evidence_dom.get_text(" ", strip=True)
    if len(html) > 8000 or len(text) < 40:
        return
    binding, identity = compact_binding(state["binding"], state["identity"])
    state["evidence"] = {
        "type": "detail_dom", "kind": state["kind"], "binding": binding, "identity": identity,
        "html": html, "expected_text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "expected": {"source_job_id": doc.source_job_id, "title": doc.title,
                     "url": clean_url(doc.url), "recruiting_entity": doc.recruiting_entity},
    }


def capture_response(payload, mapping, *, state=None):
    """Only prove moved list paths whose existing public field contract still identifies jobs."""
    # Browser event callbacks must pass the collection's state explicitly because
    # Playwright's EventGreenlet does not inherit the caller's ContextVars.
    state = _capture.get() if state is None else state
    if state is None or state["evidence"]:
        return
    from app.platform_collectors import extract_jobs, path_value

    lists, visits = [], [0]
    def visit(node, path=()):
        visits[0] += 1
        if len(path) > 6 or visits[0] > 300 or len(lists) >= 5:
            return
        if isinstance(node, list) and node and isinstance(node[0], dict):
            lists.append((path, node[:2]))
        elif isinstance(node, dict):
            for key, value in list(node.items())[:40]:
                if isinstance(key, str) and not PRIVATE_KEY.search(key) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
                    visit(value, (*path, key))
    visit(payload)
    for path, rows in lists:
        if not path or list(path) in mapping["list_paths"]:
            continue
        trial = {**mapping, "list_paths": [list(path)]}
        parsed, _, _ = extract_jobs(payload, state["kind"], state["binding"], state["identity"], mapping=trial)
        if not parsed:
            continue
        def assign(target, keys, value):
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        safe_rows = []
        for row in rows:
            safe = {}
            for paths in mapping["fields"].values():
                for keys in paths:
                    if any(PRIVATE_KEY.search(key) for key in keys):
                        continue
                    value = path_value(row, keys)
                    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                        if isinstance(value, str):
                            value = redact_text(value[:500])
                        assign(safe, keys, value)
            safe_rows.append(safe)
        safe_payload = {}
        assign(safe_payload, path, safe_rows)
        safe_jobs, _, _ = extract_jobs(safe_payload, state["kind"], state["binding"], state["identity"], mapping=trial)
        if not safe_jobs:
            continue
        binding, identity = compact_binding(state["binding"], state["identity"])
        state["evidence"] = {
            "type": "response_list", "kind": state["kind"], "binding": binding, "identity": identity,
            "payload": safe_payload, "expected": [{"source_job_id": doc.source_job_id, "title": doc.title,
                "recruiting_entity": doc.recruiting_entity} for doc, _ in safe_jobs],
        }
        if len(json.dumps(state["evidence"], ensure_ascii=False)) > 12000:
            state["evidence"] = None
        return


def attach_evidence(error, state, base_fingerprint):
    if error.code in REPAIRABLE_ERRORS:
        error.repair_evidence = deepcopy(state["evidence"]) if state["evidence"] else None
        error.adapter_fingerprint = base_fingerprint
    return error
