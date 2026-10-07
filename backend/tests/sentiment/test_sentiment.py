import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.collection import CollectionError
from app.config import get_settings
from app.llm import ModelGateway
from app.models import ModelCall, RawDocument, Report, Subject, TaskRun
from app.notification_models import NotificationEvent
from app.sentiment_models import SentimentItem, SentimentObservation, SentimentSource, SentimentSourceRun
from app.sentiment_service import (
    SentimentDocument, canonical_url, create_sentiment_task, execute_sentiment_task,
    extract_documents, rule_classification, subject_matches,
)


def subject(db, name="星河科技"):
    obj = Subject(type="own_company", name=name, aliases=["星河产品"], keywords=["星河云"], exclusions=["游戏同名"])
    db.add(obj)
    db.commit()
    return obj


def article(text="星河科技的产品售后退款较慢，希望改进", url="https://news.example.com/post/1"):
    return '<script type="application/ld+json">' + json.dumps({
        "@type": "NewsArticle", "url": url, "headline": "星河科技客户反馈",
        "articleBody": text, "datePublished": "2026-10-01T08:00:00+08:00"}, ensure_ascii=False) + "</script>"


class Fetcher:
    def __init__(self, pages):
        self.pages = pages

    def fetch(self, url):
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return url, value

    def close(self):
        pass


def source_task(db, pages, **kwargs):
    obj = subject(db)
    source = SentimentSource(subject_id=obj.id, kind="public_web", name="公开反馈", entry_urls=list(pages))
    db.add(source)
    db.commit()
    task = create_sentiment_task(db, obj.id, **kwargs)
    return obj, source, task


def test_canonical_url_removes_tracking_not_item_identity():
    assert canonical_url("https://EXAMPLE.com/p?id=42&utm_source=x#fragment") == "https://example.com/p?id=42"
    assert canonical_url("https://xiaohongshu.com/explore/abc?xsec_token=secret") == "https://xiaohongshu.com/explore/abc"


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://user:password@example.com/p", "file:///tmp/post"])
def test_import_url_cannot_execute_or_embed_credentials(url):
    with pytest.raises(ValueError):
        SentimentDocument(url=url, content="示例文本")


def test_unknown_publication_time_is_not_guessed():
    docs, state, _ = extract_documents('<article><h1>星河科技</h1><p>星河科技发布了一款全新的软件产品。</p></article>',
                                        "https://example.com/news", "news")
    assert state is None and docs[0].published_at is None
    with pytest.raises(ValueError):
        SentimentDocument(url="https://example.com/p", content="星河科技", published_at="2026-10-01T08:00:00")


def test_keyword_exclusions_and_aliases_are_applied():
    config = {"name": "星河科技", "aliases": ["星河云"], "exclusions": ["游戏同名"]}
    assert subject_matches(config, SentimentDocument(url="https://example.com/p", content="星河云产品反馈"))[0]
    assert subject_matches(config, SentimentDocument(url="https://example.com/p", content="星河科技游戏同名")) == (False, "excluded")


def test_rule_classification_marks_unverified_clues():
    category, polarity, metadata = rule_classification("星河科技退款售后投诉，但客服态度满意")
    assert category == "customer_experience" and polarity == "mixed_clue"
    assert metadata["verification"] == "unverified"


@pytest.mark.parametrize("html,code", [
    ("<title>安全验证</title><p>拖动滑块完成验证</p>", "waiting_verification"),
    ("<p>请先登录，登录后查看内容</p>", "waiting_login"),
    ("<title>内容已删除</title>", "content_unavailable"),
])
def test_access_gate_is_not_zero_posts(html, code):
    docs, state, message = extract_documents(html, "https://weibo.com/p", "wb")
    assert not docs and state == code and message


def test_xhs_initial_state_is_json_parsed_never_executed():
    state = {"note": {"noteId": "abc123", "desc": "星河科技这个产品的质量还有很大的提升空间", "title": "反馈", "time": 1790800000000}}
    docs, _, _ = extract_documents("<script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script>",
                                     "https://www.xiaohongshu.com/explore/abc123", "xhs")
    assert len(docs) == 1 and docs[0].external_id == "abc123"
    assert docs[0].published_at.tzinfo is not None


def test_article_does_not_turn_login_overlay_into_a_post():
    docs, state, _ = extract_documents("<article>请先登录后查看星河科技相关内容</article>",
                                        "https://example.com/p", "public_web")
    assert not docs and state == "waiting_login"


def test_api_requires_authentication(anonymous_sentiment_client):
    for endpoint in ("subjects", "items", "sources", "runs", "status", "reports"):
        assert anonymous_sentiment_client.get("/api/v1/sentiment/" + endpoint).status_code == 401


def test_subject_create_patch_and_source_validation(sentiment_client):
    response = sentiment_client.post("/api/v1/sentiment/subjects", json={"name": "星河科技", "keywords": ["星河云"]})
    assert response.status_code == 201
    identifier = response.json()["id"]
    update = sentiment_client.patch("/api/v1/sentiment/subjects/" + identifier,
                                    json={"exclusions": ["游戏同名"], "aliases": ["星河"]})
    assert update.status_code == 200 and update.json()["exclusions"] == ["游戏同名"]
    bad = sentiment_client.post("/api/v1/sentiment/sources", json={
        "subject_id": identifier, "name": "微博", "kind": "wb", "entry_urls": ["https://evil.example.com"]})
    assert bad.status_code == 422
    good = sentiment_client.post("/api/v1/sentiment/sources", json={"subject_id": identifier, "name": "微博", "kind": "wb"})
    assert good.status_code == 201
    bad_patch = sentiment_client.patch("/api/v1/sentiment/subjects/" + identifier, json={"keywords": [" "]})
    assert bad_patch.status_code == 422
    bad_source_patch = sentiment_client.patch("/api/v1/sentiment/sources/" + good.json()["id"],
                                              json={"entry_urls": ["https://evil.example.com"]})
    assert bad_source_patch.status_code == 422


def test_import_is_idempotent_preserves_independent_complaints_and_history(sentiment_client, db):
    obj = subject(db)
    item = {"platform": "wb", "url": "https://weibo.com/123/abc", "title": "售后", "content": "星河科技售后退款较慢，希望尽快解决"}
    request = {"subject_id": str(obj.id), "items": [item, {**item, "url": "https://weibo.com/456/def"}]}
    first = sentiment_client.post("/api/v1/sentiment/imports", json=request)
    assert first.status_code == 201 and first.json()["counts"]["added"] == 2
    second = sentiment_client.post("/api/v1/sentiment/imports", json=request)
    assert second.json()["counts"]["unchanged"] == 2
    assert len(db.scalars(select(SentimentItem)).all()) == 2
    assert len(db.scalars(select(SentimentObservation)).all()) == 4
    assert len(db.scalars(select(NotificationEvent)).all()) == 2
    rows = sentiment_client.get("/api/v1/sentiment/items", params={"subject_id": str(obj.id)}).json()
    identifier = rows["items"][0]["id"]
    result = sentiment_client.patch("/api/v1/sentiment/items/" + identifier,
                                    json={"is_read": True, "is_favorite": True, "notes": "待核实"})
    assert result.json()["is_favorite"] is True
    request["items"][0]["content"] += "；已经收到回复"
    third = sentiment_client.post("/api/v1/sentiment/imports", json=request)
    assert third.json()["counts"]["updated"] == 1
    assert sentiment_client.get("/api/v1/sentiment/items/" + identifier).json()["is_favorite"] is True
    raw = db.scalar(select(RawDocument))
    assert raw.content_type == "application/json" and raw.sha256


def test_collection_partial_coverage_and_provenance(db):
    pages = {"https://news.example.com/a": article(), "https://news.example.com/login": "请先登录后查看"}
    obj, source, task = source_task(db, pages)
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: Fetcher(pages)) == "partial"
    db.expire_all()
    run = db.scalar(select(SentimentSourceRun))
    assert run.status == "partial" and run.coverage == "sample" and run.counts["added"] == 1
    assert run.error_code == "waiting_login"
    assert db.get(TaskRun, task.id).summary["valid_sources"] == 1
    assert not db.scalars(select(ModelCall)).all()
    item = db.scalar(select(SentimentItem))
    assert item.url == "https://news.example.com/post/1"
    assert item.published_at.astimezone(timezone.utc).hour == 0


def test_failed_source_never_claims_empty_success(db):
    pages = {"https://news.example.com/a": CollectionError("access_restricted", "来源限制访问")}
    _, source, task = source_task(db, pages)
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: Fetcher(pages)) == "failed"
    db.expire_all()
    assert db.get(SentimentSource, source.id).health == "failed"
    assert db.get(TaskRun, task.id).summary["coverage"] == "unknown"
    assert db.scalar(select(SentimentSourceRun)).counts["page_errors"][0]["code"] == "access_restricted"


def test_login_state_is_visible_without_account(db):
    pages = {"https://news.example.com/a": "<p>请先登录后查看</p>"}
    _, _, task = source_task(db, pages)
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: Fetcher(pages)) == "waiting_login"
    db.expire_all()
    assert db.get(TaskRun, task.id).status == "waiting_login"
    assert not db.scalars(select(SentimentItem)).all()


def test_page_limit_does_not_claim_full_source_coverage(db):
    pages = {f"https://news.example.com/{i}": article(url=f"https://news.example.com/p{i}") for i in range(3)}
    _, _, task = source_task(db, pages, max_pages=1)
    execute_sentiment_task(task.id, fetcher_factory=lambda _: Fetcher(pages))
    db.expire_all()
    run = db.scalar(select(SentimentSourceRun))
    assert run.pages == 1 and run.counts["configured_pages"] == 3
    assert any("预算" in note for note in run.limitations)


def test_source_scope_and_snapshot_are_bound_to_subject(db):
    pages = {"https://news.example.com/a": article()}
    a, source, _ = source_task(db, pages)
    b = subject(db, "另一家公司")
    with pytest.raises(CollectionError, match="其他对象"):
        create_sentiment_task(db, b.id, source_ids=[source.id])
    task = create_sentiment_task(db, a.id)
    a.name = "后续改名"
    db.commit()
    assert task.config_snapshot["subject"]["name"] == "星河科技"


def test_time_filter_and_unknown_time_are_reported(db):
    pages = {"https://news.example.com/a": article(), "https://news.example.com/b": "<article>星河科技发布了一个产品更新，包含全新功能。</article>"}
    _, _, task = source_task(db, pages, time_from=datetime(2026, 10, 2, tzinfo=timezone.utc))
    execute_sentiment_task(task.id, fetcher_factory=lambda _: Fetcher(pages))
    db.expire_all()
    run = db.scalar(select(SentimentSourceRun))
    assert run.counts["outside_period"] == 1 and run.counts["unknown_publication_time"] == 1
    assert any("发布时间" in value for value in run.limitations)


def test_cancelled_queued_task_never_fetches(db):
    _, _, task = source_task(db, {"https://news.example.com/a": article()})
    task.cancel_requested = True
    db.commit()
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: pytest.fail("must not fetch")) is None


def imported_subject(sentiment_client, db):
    obj = subject(db)
    response = sentiment_client.post("/api/v1/sentiment/imports", json={"subject_id": str(obj.id), "items": [{
        "url": "https://news.example.com/p", "content": "星河科技的产品售后体验受到客户讨论，希望尽快退款。"}]})
    assert response.status_code == 201
    return obj, db.scalar(select(SentimentItem))


def analysis_gateway(item_id, *, bad_citation=False):
    def response(request):
        payload = json.loads(request.content)
        assert payload["max_tokens"] <= get_settings().llm_max_output_tokens
        assert payload["messages"][0]["content"].find("不可信数据") >= 0
        analysis = {"summary": "样本中出现售后反馈，需要核实。", "findings": [{
            "text": "一条原文反馈退款问题。", "item_ids": [str(uuid.uuid4()) if bad_citation else str(item_id)],
            "category": "customer_experience"}], "limitations": ["仅有页面样本，不能推断全平台声量。"]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(analysis, ensure_ascii=False)},
                                                     "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 100, "completion_tokens": 60}})
    return ModelGateway(httpx.MockTransport(response))


def test_bounded_analysis_cites_items_and_emits_report_once(sentiment_client, db):
    obj, item = imported_subject(sentiment_client, db)
    task = create_sentiment_task(db, obj.id, mode="analyze")
    assert execute_sentiment_task(task.id, gateway=analysis_gateway(item.id)) == "succeeded"
    db.expire_all()
    report = db.scalar(select(Report))
    assert report.domain == "sentiment" and report.evidence[0]["item_id"] == str(item.id)
    assert report.coverage["analysis_sample"] == 1 and report.coverage["matched_items"] == 1
    assert len(db.scalars(select(ModelCall)).all()) == 1
    assert db.scalar(select(NotificationEvent).where(NotificationEvent.type == "report.ready"))
    execute_sentiment_task(task.id, gateway=analysis_gateway(item.id))
    assert len(db.scalars(select(ModelCall)).all()) == 1


def test_analysis_rejects_invented_citation(sentiment_client, db):
    obj, item = imported_subject(sentiment_client, db)
    task = create_sentiment_task(db, obj.id, mode="analyze")
    assert execute_sentiment_task(task.id, gateway=analysis_gateway(item.id, bad_citation=True)) == "failed"
    db.expire_all()
    assert db.get(TaskRun, task.id).error_code == "invalid_citation"
    assert not db.scalars(select(Report)).all()
    assert len(db.scalars(select(ModelCall)).all()) == 1


def test_analysis_budget_denies_without_network(sentiment_client, db, monkeypatch):
    obj, item = imported_subject(sentiment_client, db)
    monkeypatch.setattr(get_settings(), "llm_daily_request_limit", 0)
    task = create_sentiment_task(db, obj.id, mode="analyze")
    gateway = ModelGateway(httpx.MockTransport(lambda _: pytest.fail("budget must deny before network")))
    assert execute_sentiment_task(task.id, gateway=gateway) == "failed"
    db.expire_all()
    assert db.get(TaskRun, task.id).error_code == "daily_budget"
    assert not db.scalars(select(ModelCall)).all()


def test_no_evidence_does_not_spend_model_budget(db):
    obj = subject(db)
    task = create_sentiment_task(db, obj.id, mode="analyze")
    assert execute_sentiment_task(task.id) == "failed"
    db.expire_all()
    assert db.get(TaskRun, task.id).error_code == "no_evidence"
    assert not db.scalars(select(ModelCall)).all()


def test_module_status_is_honest_about_bettafish(sentiment_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "bettafish_repo_dir", "")
    result = sentiment_client.get("/api/v1/sentiment/status")
    assert result.status_code == 200
    assert result.json()["bettafish"]["status"] == "not_configured"
