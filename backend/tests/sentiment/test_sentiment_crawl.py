from types import SimpleNamespace
import uuid
from urllib.parse import unquote

import pytest
from sqlalchemy import select

from app.collection import CollectionError
from app.config import get_settings
from app.models import SourceAccount, TaskRun
from app.sentiment_models import SentimentItem, SentimentSource, SentimentSourceRun
from app.sentiment_service import SentimentBrowserFetcher, collect_sentiment_pages, create_sentiment_task, default_urls, execute_sentiment_task, extract_documents, parse_date, SentimentDocument, subject_matches
from test_sentiment import Fetcher, article, source_task, subject


def test_search_follows_observed_detail_and_next_link_with_one_total_budget():
    entry = "https://www.xiaohongshu.com/search_result?keyword=星河科技"
    detail = "https://www.xiaohongshu.com/explore/abc123?xsec_token=test-navigation-only"
    next_page = "https://www.xiaohongshu.com/search_result?keyword=星河科技&page=2"
    pages = {entry: f'<a href="{detail}">星河科技服务体验</a><a rel="next" href="{next_page}">下一页</a>',
             detail: '<div id="detail-title">星河科技体验</div><div id="detail-desc">星河科技售后退款比较慢，希望尽快收到回复。</div>',
             next_page: '<a href="/explore/def456">另一条星河科技的具体反馈</a>'}
    fetcher = Fetcher(pages)
    result = collect_sentiment_pages({"kind": "xhs", "entry_urls": [entry]}, {"name": "星河科技"},
                                      max_pages=3, fetcher=fetcher)
    assert result.attempted_pages == result.successful_pages == 3
    assert result.detail_pages == 1 and result.pending_pages == 1
    assert len(result.documents) == 1 and result.documents[0].content_extent == "detail_body"
    assert result.documents[0].external_id == "abc123"
    assert "test-navigation-only" not in result.documents[0].source_page_url
    assert all("test-navigation-only" not in url for url in result.visited_urls)


def test_detail_failure_keeps_search_sample_and_does_not_claim_full_body():
    entry = "https://s.weibo.com/weibo?q=星河科技"
    detail = "https://weibo.com/123/abc"
    pages = {entry: f'<div class="card-wrap"><p class="txt">星河科技产品出现故障，客户正在等待答复。</p><p class="from"><a href="{detail}">日期</a></p></div>',
             detail: CollectionError("waiting_login", "详情要求登录")}
    result = collect_sentiment_pages({"kind": "wb", "entry_urls": [entry]}, {"name": "星河科技"},
                                      max_pages=2, fetcher=Fetcher(pages))
    assert result.attempted_pages == 2 and result.successful_pages == 1
    assert result.documents[0].content_extent == "search_excerpt"
    assert result.errors[0]["code"] == "waiting_login"


def test_crawl_does_not_follow_other_platform_or_repeat_cyclic_next_page():
    entry = "https://s.weibo.com/weibo?q=星河科技"
    html = f'<a href="https://evil.example.com/article/1">星河科技的客户体验报道</a><a rel="next" href="{entry}">下一页</a>'
    result = collect_sentiment_pages({"kind": "wb", "entry_urls": [entry]}, {"name": "星河科技"},
                                      max_pages=5, fetcher=Fetcher({entry: html}))
    assert result.attempted_pages == 1 and result.errors[0]["code"] == "no_extractable_content"


def test_budget_includes_failed_detail_attempts():
    entry = "https://s.weibo.com/weibo?q=星河科技"
    detail = "https://weibo.com/123/abc"
    pages = {entry: f'<a href="{detail}">星河科技售后投诉详情</a><a rel="next" href="/weibo?q=星河科技&page=2">下一页</a>',
             detail: CollectionError("http_error", "404")}
    result = collect_sentiment_pages({"kind": "wb", "entry_urls": [entry]}, {"name": "星河科技"},
                                      max_pages=2, fetcher=Fetcher(pages))
    assert result.attempted_pages == 2 and result.successful_pages == 1 and result.pending_pages == 1
    assert not result.documents and result.errors[0]["code"] == "http_error"


def test_video_description_is_not_presented_as_audio_transcript():
    import json
    html = '<script type="application/ld+json">' + json.dumps({"@type": "VideoObject",
        "name": "星河科技产品反馈", "description": "星河科技这次的售后服务体验，用户希望能尽快处理退款。",
        "url": "https://www.douyin.com/video/12345"}, ensure_ascii=False) + '</script>'
    docs, _, _ = extract_documents(html, "https://www.douyin.com/video/12345", "dy")
    assert len(docs) == 1 and docs[0].content_extent == "video_description"


def test_public_post_survives_login_required_comment_section():
    html = '<div id="detail-title">星河科技体验</div><div id="detail-desc">星河科技售后响应很慢，退款一直没有进展。</div><aside>请先登录后查看全部评论</aside>'
    docs, state, _ = extract_documents(html, "https://www.xiaohongshu.com/explore/abc123", "xhs")
    assert state == "waiting_login" and len(docs) == 1
    assert docs[0].content == "星河科技售后响应很慢，退款一直没有进展。"


def test_customer_complaint_about_login_is_not_misclassified_as_a_login_gate():
    html = '<div id="detail-title">星河科技安全验证体验</div><div id="detail-desc">星河科技一直提示请先登录，还要求拖动滑块，客户体验很差。</div>'
    docs, state, _ = extract_documents(html, "https://www.xiaohongshu.com/explore/abc123", "xhs")
    assert state is None and len(docs) == 1


def test_short_identified_customer_post_is_not_dropped_as_navigation():
    html = '<div id="detail-desc">星河科技避雷</div>'
    docs, _, _ = extract_documents(html, "https://www.xiaohongshu.com/explore/abc123", "xhs")
    assert docs[0].content == "星河科技避雷"


def test_weibo_date_preserves_explicit_timezone_without_guessing_relative_time():
    assert parse_date("Sat Oct 03 21:03:02 +0800 2026").isoformat() == "2026-10-03T21:03:02+08:00"
    assert parse_date("刚刚") is None


def test_topic_keyword_alone_cannot_attribute_every_refund_to_a_company():
    config = {"name": "星河科技", "aliases": ["星河云"], "keywords": ["退款"], "exclusions": []}
    doc = SentimentDocument(url="https://example.com/post/1", content="另一家公司的退款迟迟没有到账。")
    assert subject_matches(config, doc) == (False, "unmatched")
    urls = [unquote(url) for url in default_urls("wb", config)]
    assert urls[0].endswith("q=星河科技")
    assert any(url.endswith("q=星河科技 退款") for url in urls)
    assert not any(url.endswith("q=退款") for url in urls)


def test_manual_import_can_attribute_a_selected_customer_post_without_entity_name(sentiment_client, db):
    obj = subject(db)
    response = sentiment_client.post("/api/v1/sentiment/imports", json={"subject_id": str(obj.id), "items": [{
        "url": "https://example.com/post/1", "content": "这款产品售后退款很慢，一直没有收到回复。"}]})
    assert response.status_code == 201 and response.json()["counts"]["added"] == 1
    item = db.scalar(select(SentimentItem))
    assert item.classification["attribution_basis"] == "user_import"
    assert item.classification["attribution_verified"] is False


def test_explicit_single_url_attribution_does_not_apply_to_discovered_posts(sentiment_client, db):
    obj = subject(db)
    entry = "https://news.example.com/post/1"
    other = "https://news.example.com/post/2"
    source_response = sentiment_client.post("/api/v1/sentiment/sources", json={
        "subject_id": str(obj.id), "kind": "public_web", "name": "已选定的单篇反馈", "entry_urls": [entry],
        "attribution_basis": "user_selected_url"})
    assert source_response.status_code == 201
    pages = {entry: '<article><h1>售后反馈</h1>这款产品退款太慢，一直没有收到客服回复。</article>'
                   f'<a href="{other}">其他公司的客户退款相关报道</a>',
             other: '<article><h1>其他客户反馈</h1>另一个商家的服务很差，售后流程完全没有回应。</article>'}
    task = create_sentiment_task(db, obj.id, max_pages=2)
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: Fetcher(pages)) == "succeeded"
    db.expire_all()
    items = db.scalars(select(SentimentItem)).all()
    assert len(items) == 1 and items[0].url == entry
    assert items[0].classification["attribution_basis"] == "user_selected_url"
    run = db.scalar(select(SentimentSourceRun))
    assert run.counts["unmatched"] == 1 and run.counts["pending_attribution"][0]["url"] == other


def test_user_selected_attribution_rejects_search_or_whole_site(sentiment_client, db):
    obj = subject(db)
    for kind, url in [("wb", "https://s.weibo.com/weibo?q=退款"), ("public_web", "https://news.example.com/")]:
        response = sentiment_client.post("/api/v1/sentiment/sources", json={"subject_id": str(obj.id),
            "name": kind, "kind": kind, "entry_urls": [url], "attribution_basis": "user_selected_url"})
        assert response.status_code == 422


def test_detail_page_does_not_misattribute_embedded_recommended_notes():
    import json
    payload = {"notes": [{"noteId": "abc123", "desc": "已选定作品的公开客户反馈正文。"},
                         {"noteId": "def456", "desc": "其他商家的推荐作品不能归入这一条。"}]}
    html = "<script>window.__INITIAL_STATE__=" + json.dumps(payload, ensure_ascii=False) + "</script>"
    docs, _, _ = extract_documents(html, "https://www.xiaohongshu.com/explore/abc123", "xhs")
    assert len(docs) == 1 and docs[0].external_id == "abc123"


def test_generated_searches_record_unread_terms_and_rotate_across_runs(db):
    obj = subject(db)
    source = SentimentSource(subject_id=obj.id, kind="wb", name="自动关注词", entry_urls=[])
    db.add(source)
    db.commit()
    visited = []
    class SearchFetcher:
        def fetch(self, url):
            visited.append(url)
            return url, article(url="https://weibo.com/123/abc")
        def close(self):
            pass
    for _ in range(2):
        task = create_sentiment_task(db, obj.id, max_pages=1)
        assert execute_sentiment_task(task.id, fetcher_factory=lambda _: SearchFetcher()) == "partial"
        db.expire_all()
    queries = [unquote(url) for url in visited]
    assert queries[0].endswith("q=星河科技") and queries[1].endswith("q=星河产品")
    runs = db.scalars(select(SentimentSourceRun).order_by(SentimentSourceRun.created_at)).all()
    assert runs[0].counts["search_queries"][0]["status"] == "page_read"
    assert any(query["status"] == "not_attempted" for query in runs[0].counts["search_queries"])
    assert runs[0].counts["pending_urls"]


def test_login_gate_stops_remaining_alias_queries_without_advancing_cursor(db):
    obj = subject(db)
    source = SentimentSource(subject_id=obj.id, kind="wb", name="未登录关注词", entry_urls=[])
    db.add(source)
    db.commit()
    visited = []
    class LoginFetcher:
        def fetch(self, url):
            visited.append(url)
            return url, "<p>请先登录后查看全部内容</p>"
        def close(self):
            pass
    task = create_sentiment_task(db, obj.id, max_pages=5)
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: LoginFetcher()) == "waiting_login"
    db.expire_all()
    assert len(visited) == 1 and not db.get(SentimentSource, source.id).config.get("search_cursor")
    run = db.scalar(select(SentimentSourceRun))
    assert run.counts["search_queries"][0]["status"] == "waiting_login"
    assert run.counts["search_queries"][1]["status"] == "not_attempted"


@pytest.mark.parametrize("render,expected", [(None, "browser"), (True, "browser"), (False, "http")])
def test_social_render_mode_respects_explicit_http_choice(db, monkeypatch, render, expected):
    obj = subject(db)
    url = "https://weibo.com/123/abc"
    source = SentimentSource(subject_id=obj.id, kind="wb", name="微博", entry_urls=[url])
    db.add(source)
    db.commit()
    selected = []
    def browser(*args, **kwargs):
        selected.append("browser")
        return Fetcher({url: article(url=url)})
    def http():
        selected.append("http")
        return Fetcher({url: article(url=url)})
    monkeypatch.setattr("app.sentiment_service.SentimentBrowserFetcher", browser)
    monkeypatch.setattr("app.sentiment_service.PublicFetcher", http)
    task = create_sentiment_task(db, obj.id, render=render)
    assert execute_sentiment_task(task.id) == "succeeded"
    assert selected == [expected]


def test_retry_refreshes_source_account_after_human_login(sentiment_client, db):
    obj = subject(db)
    source = SentimentSource(subject_id=obj.id, kind="wb", name="微博")
    account = SourceAccount(kind="wb", name="刚完成登录", health="saved")
    db.add_all([source, account])
    db.commit()
    created = sentiment_client.post("/api/v1/sentiment/runs", json={"subject_id": str(obj.id)}).json()
    old = db.get(TaskRun, uuid.UUID(created["task_id"]))
    old.status = "waiting_login"
    source.account_id = account.id
    db.commit()
    response = sentiment_client.post(f"/api/v1/sentiment/runs/{old.id}/retry")
    assert response.status_code == 202
    new = db.get(TaskRun, uuid.UUID(response.json()["task_id"]))
    assert old.config_snapshot["sources"][0]["account_id"] is None
    assert new.config_snapshot["sources"][0]["account_id"] == str(account.id)


def test_time_budget_preserves_already_read_source_sample(db, monkeypatch):
    pages = {"https://news.example.com/a": article(), "https://news.example.com/b": article()}
    _, _, task = source_task(db, pages)
    class ExpiringFetcher(Fetcher):
        def fetch(self, url):
            result = super().fetch(url)
            monkeypatch.setattr(get_settings(), "task_timeout_seconds", 0)
            return result
    assert execute_sentiment_task(task.id, fetcher_factory=lambda _: ExpiringFetcher(pages)) == "partial"
    db.expire_all()
    assert len(db.scalars(select(SentimentItem)).all()) == 1
    run = db.scalar(select(SentimentSourceRun))
    assert run.status == "partial" and run.error_code == "time_budget"
    assert run.counts["attempted_pages"] == 1 and run.counts["pending_pages"] == 1


def fake_browser(monkeypatch, *, status=200, dependency=None, redirect=None):
    state = {"launches": [], "requests": [], "contexts": []}
    class Route:
        def __init__(self, request):
            self.request = request
            self.action = None
        def abort(self):
            self.action = "abort"
        def continue_(self):
            self.action = "continue"
    class Page:
        def __init__(self, context):
            self.context = context
            self.main_frame = SimpleNamespace(page=self)
        def goto(self, url, **kwargs):
            self.url = redirect or url
            for current, resource_type in [(url, "document"), *([(dependency, "script")] if dependency else []),
                                            *([(redirect, "document")] if redirect else [])]:
                request = SimpleNamespace(url=current, resource_type=resource_type, frame=self.main_frame,
                                           is_navigation_request=lambda kind=resource_type: kind == "document")
                route = Route(request)
                self.context.guard(route)
                state["requests"].append((current, route.action))
            return SimpleNamespace(status=status)
        def wait_for_load_state(self, *args, **kwargs):
            pass
        def content(self):
            return "<article>星河科技产品服务的公开反馈正文，客户希望继续改进。</article>"
    class Context:
        def __init__(self, **kwargs):
            state["contexts"].append(kwargs)
        def route(self, pattern, guard):
            self.guard = guard
        def route_web_socket(self, pattern, guard):
            pass
        def new_page(self):
            return Page(self)
        def close(self):
            pass
    class Browser:
        def new_context(self, **kwargs):
            return Context(**kwargs)
        def close(self):
            pass
    class Chromium:
        def launch(self, **kwargs):
            state["launches"].append(kwargs)
            return Browser()
    class Playwright:
        chromium = Chromium()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr("playwright.sync_api.sync_playwright", Playwright)
    return state


def test_browser_pins_every_validated_host_before_continuing(monkeypatch):
    state = fake_browser(monkeypatch, dependency="https://cdn.example.com/script.js")
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda url: "93.184.216.34" if "cdn." not in url else "93.184.216.35")
    fetcher = SentimentBrowserFetcher(platform="public_web")
    fetcher.fetch("https://news.example.com/article")
    assert len(state["launches"]) == 2
    assert "MAP news.example.com 93.184.216.34" in state["launches"][0]["args"][0]
    assert "MAP cdn.example.com 93.184.216.35" in state["launches"][1]["args"][0]
    assert state["requests"][1] == ("https://cdn.example.com/script.js", "abort")
    assert state["requests"][-1] == ("https://cdn.example.com/script.js", "continue")


def test_browser_blocks_private_dependency_before_second_navigation(monkeypatch):
    state = fake_browser(monkeypatch, dependency="http://169.254.169.254/latest")
    def validate(url):
        if "169.254" in url:
            raise CollectionError("restricted_address", "private")
        return "93.184.216.34"
    monkeypatch.setattr("app.collection.validate_fetch_url", validate)
    with pytest.raises(CollectionError, match="private"):
        SentimentBrowserFetcher().fetch("https://news.example.com/article")
    assert len(state["launches"]) == 1 and state["requests"][-1][1] == "abort"


@pytest.mark.parametrize("status,code", [(401, "waiting_login"), (403, "access_restricted"), (404, "http_error"), (500, "http_error")])
def test_browser_error_page_cannot_become_a_post(monkeypatch, status, code):
    fake_browser(monkeypatch, status=status)
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda _: "93.184.216.34")
    with pytest.raises(CollectionError) as error:
        SentimentBrowserFetcher().fetch("https://news.example.com/article")
    assert error.value.code == code


def test_browser_session_filters_other_platform_state_and_blocks_navigation(db, monkeypatch):
    account = SourceAccount(kind="wb", name="微博登录", health="saved")
    db.add(account)
    db.commit()
    monkeypatch.setattr("app.browser_sessions.load_state", lambda _: {
        "cookies": [{"name": "wb", "domain": ".weibo.com"}, {"name": "private_other", "domain": ".zhipin.com"}],
        "origins": [{"origin": "https://weibo.com", "localStorage": []},
                    {"origin": "https://evil.example.com", "localStorage": [{"name": "secret", "value": "never-forward"}]}]})
    fetcher = SentimentBrowserFetcher(account.id, platform="wb")
    filtered = fetcher._scoped_state()
    assert [cookie["name"] for cookie in filtered["cookies"]] == ["wb"]
    assert [origin["origin"] for origin in filtered["origins"]] == ["https://weibo.com"]
    state = fake_browser(monkeypatch, redirect="https://evil.example.com/read")
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda _: "93.184.216.34")
    with pytest.raises(CollectionError) as error:
        fetcher.fetch("https://weibo.com/123/abc")
    assert error.value.code == "source_host_mismatch"
    assert state["requests"][-1] == ("https://evil.example.com/read", "abort")
    assert len(state["contexts"][0]["storage_state"]["cookies"]) == 1


def test_authenticated_subrequests_cannot_leak_to_unregistered_provider(db, monkeypatch):
    account = SourceAccount(kind="wb", name="微博登录", health="saved")
    db.add(account)
    db.commit()
    monkeypatch.setattr("app.browser_sessions.load_state", lambda _: {"cookies": [], "origins": []})
    state = fake_browser(monkeypatch, dependency="https://evil.example.com/collect?value=private")
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda _: "93.184.216.34")
    fetcher = SentimentBrowserFetcher(account.id, platform="wb")
    assert not fetcher._navigation_allowed("http://weibo.com/")
    fetcher.fetch("https://weibo.com/123/abc")
    assert state["requests"][-1][1] == "abort"
    assert fetcher.render_warnings[0]["code"] == "account_dependency_blocked"


def test_readable_public_body_is_preserved_when_dependency_budget_is_exhausted(monkeypatch):
    fake_browser(monkeypatch, dependency="https://cdn.example.com/script.js")
    monkeypatch.setattr("app.collection.validate_fetch_url", lambda _: "93.184.216.34")
    fetcher = SentimentBrowserFetcher(platform="public_web", max_host_rounds=1)
    _, html = fetcher.fetch("https://news.example.com/article")
    assert "星河科技" in html
    assert fetcher.render_warnings[0]["code"] == "browser_dependency_partial"
    assert fetcher.navigation_attempts == 1


def test_source_failure_message_survives_task_finalization(db):
    pages = {'https://feedback.example/item': CollectionError('waiting_login','来源需要登录')}
    _, _, task = source_task(db,pages)
    execute_sentiment_task(task.id,fetcher_factory=lambda *_:Fetcher(pages))
    db.refresh(task)
    assert task.status == 'waiting_login'
    assert task.error_code == 'waiting_login' and task.error_message == '来源需要登录'


def test_douyin_dependency_budget_is_bounded_and_other_sources_keep_existing_limit():
    assert SentimentBrowserFetcher(platform='dy').host_limit == 32
    assert SentimentBrowserFetcher(platform='dy').max_host_rounds == 6
    assert SentimentBrowserFetcher(platform='xhs').host_limit == 12
    assert SentimentBrowserFetcher(platform='xhs').max_host_rounds == 3
    assert SentimentBrowserFetcher(platform='dy',max_host_rounds=2).max_host_rounds == 2


def test_douyin_captcha_interstitial_requires_human_verification_not_zero_results():
    from app.sentiment_service import page_access_state
    assert page_access_state('<html><title>验证码中间页</title><body></body></html>', 'https://www.douyin.com/search/name')[0] == 'waiting_verification'
    assert page_access_state('<html><title>客户评价</title><article>星河科技网站的验证码中间页不好用</article></html>')[0] is None
