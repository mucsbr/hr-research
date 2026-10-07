import json

import httpx
import pytest

from app.collection import CollectionError, JobDocument, PublicFetcher, jsonld_jobs, matches_filters, validate_fetch_url


def test_jsonld_preserves_source_and_missing_salary():
    job = {"@type": "JobPosting", "title": "算法工程师", "identifier": {"value": "42"},
           "description": "<p>机器学习研发</p>", "url": "/jobs/42",
           "jobLocation": {"address": {"addressCountry": "中国", "addressLocality": "上海"}}}
    html = '<script type="application/ld+json">' + json.dumps({"@graph": [job]}) + "</script>"
    result = jsonld_jobs(html, "https://example.com/jobs")
    assert len(result) == 1
    assert result[0].source_job_id == "42"
    assert result[0].url == "https://example.com/jobs/42"
    assert result[0].locations == ["中国 上海"]
    assert result[0].salary_raw is None
    assert result[0].description == "机器学习研发"


def test_login_page_is_not_a_job():
    assert jsonld_jobs("<h1>登录查看完整内容</h1>", "https://example.com") == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://169.254.169.254",
                                   "http://user:password@example.com"])
def test_reject_nonpublic_or_credential_urls(url):
    with pytest.raises(CollectionError):
        validate_fetch_url(url)


def test_scope_filters_are_deterministic():
    job = JobDocument(source_job_id="a", title="Python 后端研发", url="https://example.com",
                      locations=["上海"], description="平台开发")
    assert matches_filters(job, {"cities": ["上海"], "keywords": ["python"]})
    assert not matches_filters(job, {"cities": ["北京"]})
    assert not matches_filters(job, {"exclusions": ["后端"]})


def test_public_fetch_pins_ip_and_preserves_tls_hostname(monkeypatch):
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda _: "93.184.216.34")
    def handle(request):
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "jobs.example.com"
        assert request.extensions["sni_hostname"] == "jobs.example.com"
        return httpx.Response(200, text="<p>公开岗位</p>")
    fetcher = PublicFetcher(transport=httpx.MockTransport(handle))
    try:
        url, body = fetcher.fetch("https://jobs.example.com/list")
        assert url == "https://jobs.example.com/list"
        assert "公开岗位" in body
    finally:
        fetcher.close()


@pytest.mark.parametrize('resolver', ['alidns', 'google'])
def test_tun_dns_uses_configured_resolver_and_keeps_public_address_validation(monkeypatch,resolver):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(),'public_dns_fallback',True)
    monkeypatch.setattr(get_settings(),'public_dns_resolver',resolver)
    monkeypatch.setattr('app.collection.socket.getaddrinfo',lambda *_:[(2,1,6,'',('198.18.0.12',443))])
    def resolve(url,**kwargs):
        assert url == ('https://dns.alidns.com/resolve' if resolver == 'alidns' else 'https://dns.google/resolve')
        assert kwargs['trust_env'] is False
        return httpx.Response(200,json={'Answer':[{'type':1,'data':'127.0.0.1'}]},request=httpx.Request('GET',url))
    monkeypatch.setattr('app.collection.httpx.get',resolve)
    with pytest.raises(CollectionError) as failure:
        validate_fetch_url('https://resolver-test.example.invalid')
    assert failure.value.code == 'restricted_address'
