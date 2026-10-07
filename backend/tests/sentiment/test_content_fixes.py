"""Regression evidence for retained source detail and bounded news partial results."""
import json
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from urllib.parse import unquote

import pytest
from sqlalchemy import func, select, text

from app.config import get_settings
from app.db import session_factory
from app.models import ModelCall, RawDocument, Subject, TaskRun, now
from app.news_models import NewsItem, NewsObservation, NewsSource, NewsSourceRun
from app.news_service import create_news_task, execute_news_task
from app.notification_models import NotificationDelivery, NotificationEvent
from app.sentiment_models import SentimentItem, SentimentObservation
from app.sentiment_service import create_sentiment_task, evidence_for_analysis


def import_document(client, subject, document):
    response = client.post("/api/v1/sentiment/imports", json={"subject_id": str(subject.id), "items": [document]})
    assert response.status_code == 201
    return response.json()


def watched_subject(db):
    subject = Subject(type="own_company", name="星河科技", aliases=[], keywords=[], exclusions=[])
    db.add(subject)
    db.commit()
    return subject


@pytest.mark.parametrize("lesser_extent", ["search_excerpt", "source_text", "video_description"])
def test_detail_survives_lower_extent_observation_with_independent_raw_evidence(db, sentiment_client, lesser_extent):
    subject = watched_subject(db)
    full = {"platform": "wb", "url": "https://m.weibo.cn/detail/42", "external_id": "42",
            "title": "客户反馈", "content": "星河科技产品故障尚未解决，售后处理仍在进行。" * 12,
            "author": "公开作者", "published_at": "2026-10-01T08:00:00+08:00",
            "content_extent": "detail_body", "source_page_url": "https://m.weibo.cn/detail/42", "metrics": {"likes": 3}}
    first = import_document(sentiment_client, subject, full)
    db.expire_all()
    initial = db.scalar(select(SentimentItem))
    content_hash, published_at = initial.content_hash, initial.published_at
    snippet = {**full, "title": "", "content": "星河科技产品故障……", "author": None, "published_at": None,
               "metrics": {}, "content_extent": lesser_extent, "source_page_url": "https://s.weibo.com/weibo?q=星河科技"}
    second = import_document(sentiment_client, subject, snippet)
    db.expire_all()
    item = db.scalar(select(SentimentItem))
    assert item.content == full["content"] and item.title == full["title"]
    assert item.content_hash == content_hash and item.published_at == published_at
    assert item.author == full["author"] and item.metrics == full["metrics"]
    assert item.classification["content_extent"] == "detail_body"
    assert item.classification["source_page_url"] == full["source_page_url"]
    assert second["counts"]["retained_primary"] == 1 and second["counts"]["updated"] == 0
    observations = db.scalars(select(SentimentObservation).order_by(SentimentObservation.created_at)).all()
    assert len(observations) == 2
    assert [o.snapshot["content_extent"] for o in observations] == ["detail_body", lesser_extent]
    raw = db.get(RawDocument, observations[1].raw_document_id)
    assert unquote(json.loads(Path(raw.storage_ref).read_text())["source_page_url"]) == snippet["source_page_url"]
    task = create_sentiment_task(db, subject.id, mode="analyze")
    evidence, _ = evidence_for_analysis(db, task)
    assert evidence[0]["content"] == full["content"]
    assert evidence[0]["content_source_run_id"] == str(observations[0].source_run_id)
    assert evidence[0]["latest_observation_extent"] == lesser_extent
    assert evidence[0]["content_observed_at"] == item.classification["content_observed_at"]
    assert first["task_id"] != second["task_id"]
    assert len(db.scalars(select(NotificationEvent)).all()) == 1
    assert not db.scalars(select(NotificationDelivery)).all() and not db.scalars(select(ModelCall)).all()


def test_newer_detail_can_be_shorter_and_upgrade_excerpt_without_inventing_metadata(db, sentiment_client):
    subject = watched_subject(db)
    doc = {"platform": "wb", "url": "https://m.weibo.cn/detail/42", "external_id": "42",
           "title": "客户反馈", "content": "星河科技搜索页面的冗长摘要。" * 20,
           "content_extent": "search_excerpt", "published_at": "2026-10-01T08:00:00+08:00"}
    import_document(sentiment_client, subject, doc)
    detail = {**doc, "content": "星河科技已修复故障，用户表示接受处理。", "content_extent": "detail_body"}
    import_document(sentiment_client, subject, detail)
    revised = {**detail, "content": "星河科技处理完成。", "published_at": None}
    response = import_document(sentiment_client, subject, revised)
    db.expire_all()
    item = db.scalar(select(SentimentItem))
    assert item.content == revised["content"] and item.classification["content_extent"] == "detail_body"
    assert item.published_at is not None and response["counts"]["updated"] == 1
    assert len(db.scalars(select(SentimentObservation)).all()) == 3


def news_source(db, *, kind="web"):
    source = NewsSource(name="独立预算回归来源", kind=kind, url="https://hr.example.com/news/", source_type="official")
    db.add(source)
    db.commit()
    return source


class BudgetFetcher:
    def __init__(self, source, clock, task_id, *, interruption=None, feed=False):
        self.calls, self.clock, self.task_id = [], clock, task_id
        self.interruption, self.feed, self.closed = interruption, feed, False
        self.article = "https://hr.example.com/news/first.html"
        body = "用人单位应依法保障劳动者合法权益。" * 8
        self.pages = {source.url: f'<a href="{self.article}">第一篇劳动政策正式通知</a><a href="/news/second.html">第二篇劳动政策正式通知</a>',
                      self.article: '<h1>关于劳动关系的通知</h1><article>' + body + '</article>'}
        if feed:
            self.pages[source.url] = '<rss version="2.0"><channel><title>HR</title><item><title>关于劳动关系的通知</title><link>' + self.article + '</link><description>' + body + '</description></item></channel></rss>'

    def fetch(self, url):
        self.calls.append(url)
        if url == self.article or self.feed:
            self.clock[0] += timedelta(seconds=120)
            if self.interruption:
                with session_factory()() as other:
                    task = other.get(TaskRun, self.task_id)
                    if self.interruption == "cancelled":
                        task.cancel_requested = True
                    else:
                        task.attempt += 1
                    other.commit()
        return url, self.pages[url]

    def close(self):
        self.closed = True


@pytest.mark.parametrize("feed", [False, True])
def test_news_time_budget_persists_read_articles_and_skips_unattempted_sources(db, monkeypatch, feed):
    import app.news_service as service
    clock = [now()]
    monkeypatch.setattr(service, "now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "task_timeout_seconds", 60)
    source = news_source(db, kind="feed" if feed else "web")
    later = NewsSource(name="未读取来源", kind="feed", url="https://later.example.com/feed")
    db.add(later)
    db.commit()
    task = create_news_task(db, [source.id, later.id], max_pages=3)
    fetcher = BudgetFetcher(source, clock, task.id, feed=feed)
    def factory(frozen):
        assert frozen["id"] == str(source.id), "deadline must not start another source"
        return fetcher
    assert execute_news_task(task.id, fetcher_factory=factory) == "partial"
    db.expire_all()
    run = db.scalar(select(NewsSourceRun))
    finished = db.get(TaskRun, task.id)
    assert run.status == "partial" and run.error_code == "time_budget"
    assert run.counts["added"] == 1 and run.counts["pages"] == (1 if feed else 2)
    assert finished.summary["unattempted_sources"] == 1 and finished.error_code == "time_budget"
    assert len(db.scalars(select(NewsItem)).all()) == 1
    assert len(db.scalars(select(NewsObservation)).all()) == 1
    assert fetcher.closed and len(fetcher.calls) == (1 if feed else 2)
    assert not db.scalars(select(ModelCall)).all() and not db.scalars(select(NotificationDelivery)).all()


@pytest.mark.parametrize("interruption", ["cancelled", "execution_superseded"])
def test_news_deadline_never_overrides_cancellation_or_new_owner(db, monkeypatch, interruption):
    import app.news_service as service
    clock = [now()]
    monkeypatch.setattr(service, "now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "task_timeout_seconds", 60)
    source = news_source(db)
    task = create_news_task(db, [source.id], max_pages=3)
    fetcher = BudgetFetcher(source, clock, task.id, interruption=interruption)
    expected = "cancelled" if interruption == "cancelled" else "superseded"
    assert execute_news_task(task.id, fetcher_factory=lambda _: fetcher) == expected
    db.expire_all()
    active = db.get(TaskRun, task.id)
    assert active.status == ("cancelled" if interruption == "cancelled" else "running")
    if interruption == "execution_superseded":
        assert active.attempt == 2
    assert not db.scalars(select(NewsItem)).all() and not db.scalars(select(NewsObservation)).all()
    assert not db.scalars(select(NotificationEvent)).all() and fetcher.closed


@pytest.mark.parametrize("interruption", ["cancelled", "execution_superseded"])
def test_news_partial_commit_holds_task_lock_through_data_and_terminal_state(db, monkeypatch, interruption):
    """The second session must observe either the old owner or its full commit, not a gap."""
    import app.news_service as service
    clock = [now()]
    monkeypatch.setattr(service, "now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "task_timeout_seconds", 60)
    source = news_source(db)
    task = create_news_task(db, [source.id], max_pages=3)
    fetcher = BudgetFetcher(source, clock, task.id)
    started, finished = Event(), Event()
    writer_view, writer_errors, threads = {}, [], []
    original_ingest = service.ingest_news

    def competing_update():
        try:
            with session_factory()() as other:
                other.execute(text("SET LOCAL lock_timeout = '3s'"))
                started.set()
                active = other.scalar(select(TaskRun).where(TaskRun.id == task.id).with_for_update())
                writer_view.update(task_status=active.status,
                    observations=other.scalar(select(func.count()).select_from(NewsObservation)),
                    source_status=other.scalar(select(NewsSourceRun.status).where(NewsSourceRun.task_id == task.id)))
                if interruption == "cancelled":
                    active.cancel_requested = True
                else:
                    active.attempt += 1
                    active.status = "running"
                other.commit()
        except Exception as exc:
            writer_errors.append(type(exc).__name__)
        finally:
            finished.set()

    def synchronized_ingest(*args, **kwargs):
        writer = Thread(target=competing_update, daemon=True)
        threads.append(writer)
        writer.start()
        assert started.wait(2), "competing transaction did not start"
        # Never join the writer while the worker may own the row. Old code allows
        # this commit before ingest; fixed code keeps it waiting until terminal commit.
        writer_view["committed_before_ingest"] = finished.wait(0.3)
        return original_ingest(*args, **kwargs)

    monkeypatch.setattr(service, "ingest_news", synchronized_ingest)
    try:
        result = execute_news_task(task.id, fetcher_factory=lambda _: fetcher)
    finally:
        for thread in threads:
            thread.join(4)
    db.expire_all()
    active = db.get(TaskRun, task.id)
    item_count = db.scalar(select(func.count()).select_from(NewsItem))
    assert not writer_errors and finished.is_set(), writer_errors
    assert not writer_view["committed_before_ingest"], {
        "writer": writer_view, "old_result": result, "persisted_items_after_committed_change": item_count}
    assert writer_view["task_status"] == "partial" and writer_view["source_status"] == "partial", writer_view
    assert writer_view["observations"] == 1 and item_count == 1
    assert result == "partial"
    if interruption == "cancelled":
        assert active.cancel_requested and active.status == "partial"
    else:
        assert active.attempt == 2 and active.status == "running"
    assert not db.scalars(select(ModelCall)).all() and not db.scalars(select(NotificationDelivery)).all()
