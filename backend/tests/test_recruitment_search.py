"""Real discovery API/PG with explicit HTTP transports; never queries a live engine."""
import socket
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import func, select

from app import collection, recruitment_discovery, recruitment_search
from app.collection import CollectionError, PublicFetcher
from app.config import get_settings
from app.db import get_db
from app.models import AuditEvent, Company, ModelCall, SourceBinding, Subject, User
from app.recruitment_management_api import router
from app.recruitment_management_models import DiscoverySearchConfig, RecruitmentEntryCandidate
from app.recruitment_search import (
    MAX_RESPONSE_BYTES, deployment_endpoint, effective_search_configuration,
    read_search_response, validate_search_endpoint,
)
from app.security import current_user


INTERNAL = "http://recruitment-search:8080/search"
PUBLIC = "https://search.example/search"
QUERY = '"星河科技" 招聘 官网'


@pytest.fixture(autouse=True)
def search_scope(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "discovery_internal_search_url", INTERNAL)
    monkeypatch.setattr(settings, "allow_local_fixture_fetch", False)
    monkeypatch.setattr(settings, "public_dns_fallback", False)
    monkeypatch.setattr(collection, "_dns_cache", {})


class NoPublicReads:
    def __init__(self):
        self.closed = False

    def fetch(self, *_args, **_kwargs):
        pytest.fail("internal search must not fetch candidate websites or use the public reader")

    def close(self):
        self.closed = True


def body(rows=None, **extra):
    return {"query": QUERY, "results": rows if rows is not None else [], **extra}


def dns_address(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))])


@pytest.fixture
def search_api(db):
    user = User(username="search-qa", password_hash="not-used", role="admin")
    db.add(user)
    db.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: user
    with TestClient(app) as client:
        yield client


def named_company(db):
    subject = Subject(name="星河科技")
    db.add(subject)
    db.flush()
    company = Company(subject_id=subject.id, name=subject.name, website=None)
    db.add(company)
    db.commit()
    return company


def bind_search_transport(monkeypatch, handler):
    public = NoPublicReads()
    transport = httpx.MockTransport(handler)
    original = read_search_response
    monkeypatch.setattr(recruitment_discovery, "PublicFetcher", lambda: public)
    monkeypatch.setattr(recruitment_discovery, "read_search_response",
                        lambda endpoint, query, fetcher: original(endpoint, query, fetcher, transport=transport))
    return public


def test_only_exact_deployment_endpoint_is_the_internal_exception():
    assert deployment_endpoint() == INTERNAL and validate_search_endpoint(INTERNAL) == INTERNAL
    assert validate_search_endpoint(PUBLIC) == PUBLIC
    with pytest.raises(ValueError):
        validate_search_endpoint("http://127.0.0.1:8080/search")


@pytest.mark.parametrize("value", [
    "http://user:password@recruitment-search:8080/search", INTERNAL + "?key=secret",
    INTERNAL + "#other", "http://recruitment-search:8080/admin", "file:///tmp/search",
])
def test_deployment_config_cannot_embed_credentials_or_other_operations(monkeypatch, value):
    monkeypatch.setattr(get_settings(), "discovery_internal_search_url", value)
    with pytest.raises(CollectionError) as caught:
        deployment_endpoint()
    assert caught.value.code == "search_configuration_invalid"
    assert "password" not in caught.value.message and "secret" not in caught.value.message


@pytest.mark.parametrize("endpoint", [INTERNAL + "/other", "http://different-service:8080/search", "http://10.0.0.7/search"])
def test_other_private_targets_do_not_inherit_search_network_permission(monkeypatch, endpoint):
    dns_address(monkeypatch, "10.0.0.7")
    fetcher = PublicFetcher(transport=httpx.MockTransport(lambda _: pytest.fail("private target reached transport")))
    try:
        with pytest.raises(CollectionError) as caught:
            read_search_response(endpoint, QUERY, fetcher,
                                 transport=httpx.MockTransport(lambda _: pytest.fail("unapproved internal endpoint")))
        assert caught.value.code in {"search_configuration_invalid", "restricted_address"}
    finally:
        fetcher.close()


def test_normal_public_fetcher_still_rejects_even_the_fixed_internal_search_endpoint(monkeypatch):
    dns_address(monkeypatch, "172.18.0.9")
    fetcher = PublicFetcher(transport=httpx.MockTransport(lambda _: pytest.fail("ordinary fetch escaped private guard")))
    try:
        with pytest.raises(CollectionError) as caught:
            fetcher.fetch(INTERNAL)
        assert caught.value.code == "restricted_address"
    finally:
        fetcher.close()


def test_fixed_search_sends_only_query_and_uses_bounded_authenticated_free_transport(monkeypatch):
    requests, options = [], []
    real_client = httpx.Client
    monkeypatch.setattr(get_settings(), "discovery_search_timeout_seconds", 1000)
    def client(**kwargs):
        options.append(kwargs)
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        assert 1 <= kwargs["timeout"] <= 30
        return real_client(**kwargs)
    monkeypatch.setattr(recruitment_search.httpx, "Client", client)
    def answer(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == "recruitment-search"
        assert dict(request.url.params) == {"q": QUERY, "format": "json", "language": "zh-CN"}
        assert request.content == b""
        assert not any(key in request.headers for key in ["authorization", "cookie", "x-api-key", "x-worker-token"])
        return httpx.Response(200, json=body([{"url": "https://careers.example/jobs"}], number_of_results=0))
    result = read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(answer))
    assert len(requests) == len(options) == 1 and len(result.results) == 1
    assert result.evidence["request_status"] == "succeeded" and result.evidence["response_text_sha256"]


def test_test_mode_never_uses_real_internal_network_without_transport():
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads())
    assert caught.value.code == "search_test_network_forbidden"


@pytest.mark.parametrize("status,code", [(302, "search_redirect"), (307, "search_redirect"),
    (401, "search_access_restricted"), (429, "search_access_restricted"), (503, "search_http_error")])
def test_internal_http_error_or_redirect_is_not_a_candidate_result(status, code):
    calls = []
    def answer(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"location": "http://169.254.169.254/latest/meta-data/"},
                              json=body([{"url": "https://should-not-save.example/jobs"}]))
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(answer))
    assert caught.value.code == code and len(calls) == 1


@pytest.mark.parametrize("failure,code", [(httpx.ReadTimeout, "search_timeout"), (httpx.ConnectError, "search_unavailable")])
def test_internal_transport_failures_are_bounded_and_sanitized(failure, code):
    def answer(request):
        raise failure("transport-private-context", request=request)
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(answer))
    assert caught.value.code == code and "transport-private-context" not in caught.value.message


def test_streaming_search_cannot_extend_total_time_budget_with_small_chunks(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(recruitment_search.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "discovery_search_timeout_seconds", 2)
    class SlowStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"results":'
            clock[0] = 3.0
            yield b'[]}'
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=SlowStream())))
    assert caught.value.code == "search_timeout"


def test_public_search_uses_original_public_fetcher_without_candidate_navigation(monkeypatch):
    dns_address(monkeypatch, "93.184.216.34")
    calls = []
    def answer(request):
        calls.append(request)
        assert request.headers["host"] == "search.example"
        assert parse_qs(urlparse(str(request.url)).query)["q"] == [QUERY]
        return httpx.Response(200, json=body([{"url": "https://careers.example/jobs"}]))
    fetcher = PublicFetcher(transport=httpx.MockTransport(answer))
    try:
        result = read_search_response(PUBLIC, QUERY, fetcher)
        assert len(result.results) == 1 and len(calls) == 1
        assert calls[0].url.host == "93.184.216.34" and result.evidence["source_url"] == PUBLIC
    finally:
        fetcher.close()


@pytest.mark.parametrize("status,code", [(403, "access_restricted"), (503, "http_error")])
def test_public_service_errors_cannot_return_candidates(monkeypatch, status, code):
    dns_address(monkeypatch, "93.184.216.34")
    fetcher = PublicFetcher(transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body())))
    try:
        with pytest.raises(CollectionError) as caught:
            read_search_response(PUBLIC, QUERY, fetcher)
        assert caught.value.code == code
    finally:
        fetcher.close()


def test_public_search_redirect_is_rejected_before_second_request(monkeypatch):
    dns_address(monkeypatch, "93.184.216.34")
    calls = []
    def answer(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.example/search"})
    fetcher = PublicFetcher(transport=httpx.MockTransport(answer))
    try:
        with pytest.raises(CollectionError) as caught:
            read_search_response(PUBLIC, QUERY, fetcher)
        assert caught.value.code == "search_redirect" and len(calls) == 1
    finally:
        fetcher.close()


@pytest.mark.parametrize("payload,code", [
    ([], "search_contract_changed"), ({"results": {}}, "search_contract_changed"),
    (body(query="其他公司"), "search_query_mismatch"),
    (body(unresponsive_engines="not-a-list"), "search_contract_changed"),
    (body(error="upstream unavailable"), "search_upstream_failed"),
])
def test_invalid_search_contract_never_becomes_empty_success(payload, code):
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=payload)))
    assert caught.value.code == code


@pytest.mark.parametrize("content,code", [(b"x" * (MAX_RESPONSE_BYTES + 1), "search_response_too_large"),
    (b"<html>service unavailable</html>", "search_contract_changed"), (b"\xff\xff", "search_contract_changed")])
def test_search_response_size_and_encoding_budget(content, code):
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=content)))
    assert caught.value.code == code


def test_unresponsive_engines_with_no_results_is_failure_and_carries_query_evidence():
    with pytest.raises(CollectionError) as caught:
        read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=body(number_of_results=500, unresponsive_engines=[["bing", "timeout"]]))))
    assert caught.value.code == "search_upstream_failed"
    assert caught.value.search_evidence["query"] == QUERY
    assert caught.value.search_evidence["unresponsive_engines"] == [{"engine": "bing", "reason": "timeout"}]


def test_partial_results_and_inspected_rows_do_not_depend_on_number_of_results():
    rows = [{"url": f"https://careers.example/jobs/{index}"} for index in range(103)]
    result = read_search_response(INTERNAL, QUERY, NoPublicReads(), transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=body(rows, number_of_results=0, unresponsive_engines=[["bing", "timeout"]]))))
    assert len(result.results) == 100 and result.evidence["returned_results"] == 103
    assert result.evidence["request_status"] == "partial"


def test_deployment_default_and_explicit_saved_disable_have_correct_api_precedence(search_api, db, monkeypatch):
    response = search_api.get("/api/v1/settings/recruitment-discovery")
    assert response.status_code == 200
    assert response.json()["config"] == {"endpoint_url": INTERNAL, "enabled": True, "max_results": 5, "origin": "deployment"}
    saved = search_api.put("/api/v1/settings/recruitment-discovery", json={"endpoint_url": INTERNAL, "enabled": False, "max_results": 2})
    assert saved.status_code == 200
    expected = {"endpoint_url": INTERNAL, "enabled": False, "max_results": 2, "origin": "saved"}
    config = effective_search_configuration(db)
    assert {key: config[key] for key in expected} == expected
    assert config["id"] == saved.json()["id"]
    assert search_api.get("/api/v1/settings/recruitment-discovery").json()["config"] == config
    target = named_company(db)
    no_reads = NoPublicReads()
    monkeypatch.setattr(recruitment_discovery, "PublicFetcher", lambda: no_reads)
    response = search_api.post(f"/api/v1/companies/{target.id}/entry-discovery", json={})
    assert response.json()["status"] == "search_configuration_required" and no_reads.closed
    assert db.scalar(select(func.count()).select_from(RecruitmentEntryCandidate)) == 0


@pytest.mark.parametrize("endpoint", ["http://127.0.0.1:8080/search", "http://169.254.169.254/search", "http://[::1]/search"])
def test_api_rejects_unapproved_private_configuration(search_api, db, endpoint):
    response = search_api.put("/api/v1/settings/recruitment-discovery", json={"endpoint_url": endpoint})
    assert response.status_code == 422 and db.scalar(select(DiscoverySearchConfig)) is None


def test_actual_api_persists_deduplicated_candidates_without_binding_or_model_calls(search_api, db, monkeypatch):
    target = named_company(db)
    configured = search_api.put("/api/v1/settings/recruitment-discovery", json={"endpoint_url": INTERNAL, "max_results": 2})
    assert configured.status_code == 200
    rows = [None, {"url": None}, {"url": "http://127.0.0.1/admin"}, {"url": "file:///etc/passwd"},
            {"url": "https://first.example/jobs#top", "title": "招聘一"},
            {"url": "https://first.example/jobs#bottom", "title": "重复一"},
            {"url": "https://second.example/jobs", "title": "招聘二"},
            {"url": "https://third.example/jobs", "title": "超过候选预算"}]
    calls = []
    def answer(request):
        calls.append(request)
        assert request.url.params["q"] == QUERY
        return httpx.Response(200, json=body(rows, number_of_results=0, unresponsive_engines=[["bing", "timeout"]]))
    public = bind_search_transport(monkeypatch, answer)
    result = search_api.post(f"/api/v1/companies/{target.id}/entry-discovery", json={"max_candidates": 10})
    assert result.status_code == 200
    value = result.json()
    assert value["status"] == "partial" and len(value["candidates"]) == 2
    assert {item["url"] for item in value["candidates"]} == {"https://first.example/jobs", "https://second.example/jobs"}
    assert all(item["status"] == "pending" and item["evidence"]["query"] == QUERY for item in value["candidates"])
    assert len(calls) == 1 and public.closed
    assert db.scalar(select(func.count()).select_from(RecruitmentEntryCandidate)) == 2
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
    db.refresh(target)
    assert target.website is None and target.identity_status == "user_specified"


def test_failed_search_keeps_prior_candidates_and_persists_failure_audit(search_api, db, monkeypatch):
    target = named_company(db)
    existing = RecruitmentEntryCandidate(company_id=target.id, url="https://old.example/jobs", title="原候选",
                                          status="rejected", evidence={"method": "prior-evidence"})
    db.add(existing)
    db.commit()
    identifier = existing.id
    bind_search_transport(monkeypatch, lambda _: httpx.Response(200, json=body(unresponsive_engines=[["bing", "timeout"]])))
    response = search_api.post(f"/api/v1/companies/{target.id}/entry-discovery", json={})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "search_upstream_failed"
    db.expire_all()
    retained = db.get(RecruitmentEntryCandidate, identifier)
    assert retained.status == "rejected" and retained.evidence == {"method": "prior-evidence"}
    audit = db.scalar(select(AuditEvent).where(AuditEvent.action == "recruitment.discovery_failed"))
    assert audit.detail["code"] == "search_upstream_failed" and audit.detail["search"]["query"] == QUERY
    assert audit.detail["search"]["response_text_sha256"]
    assert db.scalar(select(func.count()).select_from(RecruitmentEntryCandidate)) == 1
    assert db.scalar(select(func.count()).select_from(SourceBinding)) == 0
    assert db.scalar(select(func.count()).select_from(ModelCall)) == 0
