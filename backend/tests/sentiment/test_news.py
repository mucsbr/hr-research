from datetime import date, timezone

import httpx
import pytest
from sqlalchemy import select

from app.collection import CollectionError
from app.models import ModelCall, TaskRun
from app.db import session_factory
from app.config import get_settings
from app.news_models import NewsItem, NewsObservation, NewsSource, NewsSourceRun
from app.news_service import (
    NewsDocument, classify_article, collect_news_source, create_news_task, execute_news_task,
    extract_article, extract_feed, publication_date,
)
from app.notification_models import NotificationEvent


BODY = "关于完善劳动合同管理和社会保险服务的通知。成文日期：2026年9月20日。本办法自2026年11月1日起施行。" + "用人单位应依法保障劳动者合法权益。" * 10
TITLE = "关于完善劳动合同管理的通知"


def rss(url="https://hr.example.com/article/1", body=BODY, title=TITLE):
    return f'''<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>HR政策</title>
      <link>https://hr.example.com</link><description>政策来源</description><item><title>{title}</title>
      <link>{url}</link><description>{body}</description><pubDate>Wed, 30 Sep 2026 08:30:00 +0800</pubDate>
      </item></channel></rss>'''


def html_article(body=BODY, title=TITLE):
    return f'<html><head><title>{title}</title><meta name="PubDate" content="2026-09-30"></head><body><h1>{title}</h1><article>{body}</article></body></html>'


class Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        return url, value

    def close(self):
        pass


def configured_source(db, name="配置的HR来源", url="https://hr.example.com/feed", **kwargs):
    source = NewsSource(name=name, kind="feed", url=url, source_type="official", publisher="配置的发布机构",
                         regions=["上海"], topics=["劳动关系"], **kwargs)
    db.add(source)
    db.commit()
    return source


def test_publication_time_is_separate_from_date():
    timestamp, day = publication_date("2026-10-01")
    assert timestamp is None and day == date(2026, 10, 1)
    timestamp, day = publication_date("2026-10-01T08:30:00+08:00")
    assert timestamp.astimezone(timezone.utc).hour == 0 and day == date(2026, 10, 1)
    assert publication_date("未知") == (None, None)


def test_policy_issue_effective_and_publication_dates_are_not_conflated():
    doc = NewsDocument("https://example.com/p", TITLE, BODY, published_date=date(2026, 9, 30))
    content_type, topics, dates, metadata = classify_article(doc, {"source_type": "official"})
    assert content_type == "policy_document"
    assert dates["policy_issued_date"] == date(2026, 9, 20)
    assert dates["effective_date"] == date(2026, 11, 1)
    assert "劳动关系" in topics and "社保与福利" in topics
    assert metadata["summary_type"] == "verbatim_excerpt" and metadata["legal_conclusion"] is False


def test_multiple_effective_dates_remain_uncertain():
    body = BODY + "另一项规定自2027年1月1日起施行。"
    _, _, dates, metadata = classify_article(NewsDocument("https://example.com/p", TITLE, body), {"source_type": "official"})
    assert dates["effective_date"] is None
    assert len(metadata["date_candidates"]["effective_date_candidates"]) == 2


def test_media_interpretation_does_not_become_official_policy():
    kind, _, dates, metadata = classify_article(NewsDocument("https://example.com/p", "劳动合同政策解读", BODY),
                                               {"source_type": "media"})
    assert kind == "media_interpretation" and dates["effective_date"] is None
    assert metadata["source_type_basis"] == "administrator_configuration"


def test_feed_extract_is_labeled_as_feed_summary():
    docs, count = extract_feed(rss(), "https://hr.example.com/feed", 2)
    assert count == 1 and docs[0].content_extent == "feed_summary"
    assert docs[0].published_at.astimezone(timezone.utc).hour == 0


def test_feed_dtd_or_nonfeed_is_rejected():
    with pytest.raises(CollectionError, match="DTD"):
        extract_feed('<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>', "https://example.com", 2)
    with pytest.raises(CollectionError, match="RSS"):
        extract_feed("<html>登录界面</html>", "https://example.com", 2)


def test_article_extract_uses_original_date_not_collection_date():
    doc = extract_article(html_article(), "https://hr.example.com/p")
    assert doc.title == TITLE and doc.published_date == date(2026, 9, 30)
    assert doc.published_at is None


def test_section_collection_is_same_host_and_bounded():
    pages = {
        "https://hr.example.com/section": '<a href="/news/1.html">关于劳动合同管理的通知</a><a href="/news/2.html">关于社会保险管理的通知</a><a href="https://other.example.com/news/3.html">其他网站的劳动政策报道</a>',
        "https://hr.example.com/news/1.html": html_article(),
    }
    fetcher = Fetcher(pages)
    result = collect_news_source({"url": "https://hr.example.com/section", "kind": "web"},
                                 max_pages=2, fetcher=fetcher)
    assert len(result.documents) == 1 and result.pages == 2
    assert len(fetcher.calls) == 2 and any("预算" in note for note in result.limitations)


def test_news_source_api_validates_and_does_not_seed_preferences(news_client):
    assert news_client.get("/api/v1/news/sources").json() == []
    assert news_client.post("/api/v1/news/runs", json={}).status_code == 422
    bad = news_client.post("/api/v1/news/sources", json={"name": "来源", "url": "file:///etc/passwd"})
    assert bad.status_code == 422
    result = news_client.post("/api/v1/news/sources", json={"name": "来源", "url": "https://example.com/feed",
                              "regions": ["广东"], "source_type": "official"})
    assert result.status_code == 201 and result.json()["regions"] == ["广东"]
    update = news_client.patch("/api/v1/news/sources/" + result.json()["id"], json={"keywords": ["人才"], "enabled": False})
    assert update.json()["enabled"] is False
    assert news_client.patch("/api/v1/news/sources/" + result.json()["id"], json={"url": "file:///etc/passwd"}).status_code == 422


def test_news_ingest_persists_dates_tags_and_events_without_llm(db, news_client):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    assert execute_news_task(task.id, fetcher_factory=lambda _: Fetcher({source.url: rss()})) == "succeeded"
    db.expire_all()
    item = db.scalar(select(NewsItem))
    assert item.published_date == date(2026, 9, 30) and item.effective_date == date(2026, 11, 1)
    assert item.policy_issued_date == date(2026, 9, 20) and item.regions == ["上海"]
    assert db.scalar(select(NotificationEvent)).type == "news.policy"
    assert not db.scalars(select(ModelCall)).all()
    result = news_client.get("/api/v1/news/items", params={"region": "上海", "topic": "劳动关系"}).json()
    assert result["total"] == 1
    assert news_client.get("/api/v1/news/items", params={"region": "广东"}).json()["total"] == 0
    assert news_client.patch("/api/v1/news/items/" + str(item.id), json={"is_favorite": True, "notes": "后续阅读"}).status_code == 200


def test_news_url_and_exact_content_dedup_keeps_provenance(db):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    execute_news_task(task.id, fetcher_factory=lambda _: Fetcher({source.url: rss()}))
    task2 = create_news_task(db, [source.id])
    execute_news_task(task2.id, fetcher_factory=lambda _: Fetcher({source.url: rss(url="https://hr.example.com/repost/1")}))
    db.expire_all()
    assert len(db.scalars(select(NewsItem)).all()) == 1
    assert len(db.scalars(select(NewsObservation)).all()) == 2
    assert len(db.scalars(select(NotificationEvent)).all()) == 1
    run = db.scalar(select(NewsSourceRun).where(NewsSourceRun.task_id == task2.id))
    assert run.counts["merged_content"] == 1
    task3 = create_news_task(db, [source.id])
    execute_news_task(task3.id, fetcher_factory=lambda _: Fetcher({source.url: rss(url="https://hr.example.com/repost/1", body=BODY + "另补充一项新规定。 ")}))
    assert len(db.scalars(select(NewsItem)).all()) == 2


def test_news_keyword_exclusion_and_unknown_publish_date(db):
    source = configured_source(db, keywords=["劳动合同"], exclusions=["广告"])
    task = create_news_task(db, [source.id])
    content = rss(body=BODY + "广告").replace("<pubDate>Wed, 30 Sep 2026 08:30:00 +0800</pubDate>", "")
    execute_news_task(task.id, fetcher_factory=lambda _: Fetcher({source.url: content}))
    db.expire_all()
    assert db.scalar(select(NewsSourceRun)).counts["excluded"] == 1
    assert not db.scalars(select(NewsItem)).all()


def test_news_partial_failure_preserves_good_source(db):
    good = configured_source(db)
    bad = configured_source(db, name="失败来源", url="https://hr.example.com/bad")
    task = create_news_task(db, [good.id, bad.id])
    pages = {good.url: rss(), bad.url: httpx.ConnectError("offline")}
    assert execute_news_task(task.id, fetcher_factory=lambda _: Fetcher(pages)) == "partial"
    db.expire_all()
    assert db.get(TaskRun, task.id).summary["valid_sources"] == 1
    assert db.get(NewsSource, bad.id).health == "failed"
    assert len(db.scalars(select(NewsItem)).all()) == 1


def test_news_unknown_runtime_error_marks_source_failed(db):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    assert execute_news_task(task.id, fetcher_factory=lambda _: Fetcher({source.url: RuntimeError("unexpected")})) == "failed"
    db.expire_all()
    run = db.scalar(select(NewsSourceRun))
    assert run.status == "failed" and run.error_code == "source_runtime_error"


def test_news_cancelled_before_dispatch_never_fetches(db):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    task.cancel_requested = True
    db.commit()
    assert execute_news_task(task.id, fetcher_factory=lambda _: pytest.fail("must not fetch")) is None


def test_news_recovery_reuses_successful_source_checkpoint(db):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    execute_news_task(task.id, fetcher_factory=lambda _: Fetcher({source.url: rss()}))
    db.expire_all()
    task = db.get(TaskRun, task.id)
    task.status = "queued"
    db.commit()
    assert execute_news_task(task.id, fetcher_factory=lambda _: pytest.fail("completed source must not refetch")) == "succeeded"
    assert len(db.scalars(select(NewsSourceRun)).all()) == 1
    assert len(db.scalars(select(NewsObservation)).all()) == 1
    assert len(db.scalars(select(NotificationEvent)).all()) == 1


def test_news_old_worker_stops_after_execution_generation_changes(db):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    class SupersedingFetcher(Fetcher):
        def fetch(self, url):
            with session_factory()() as other:
                active = other.get(TaskRun, task.id)
                active.attempt += 1
                other.commit()
            return super().fetch(url)
    assert execute_news_task(task.id, fetcher_factory=lambda _: SupersedingFetcher({source.url: rss()})) == "superseded"
    db.expire_all()
    active = db.get(TaskRun, task.id)
    assert active.status == "running" and active.attempt == 2
    assert not db.scalars(select(NewsItem)).all()


def test_news_enforces_time_budget_before_network(db, monkeypatch):
    source = configured_source(db)
    task = create_news_task(db, [source.id])
    monkeypatch.setattr(get_settings(), "task_timeout_seconds", 0)
    assert execute_news_task(task.id, fetcher_factory=lambda _: pytest.fail("time budget denied")) == "failed"
    db.expire_all()
    assert db.get(TaskRun, task.id).error_code == "time_budget"
