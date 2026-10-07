"""Bounded administrator-configured HR feeds/articles; all summaries are extracts."""
import hashlib
import json
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
import feedparser
import httpx
from sqlalchemy import select, text, update

from app.collection import CollectionError, PublicFetcher
from app.config import get_settings
from app.db import session_factory
from app.models import RawDocument, TaskEvent, TaskRun, now
from app.news_models import NewsItem, NewsObservation, NewsSource, NewsSourceRun
from app.notifications import emit_event, lock_notification_state
from app.sentiment_service import canonical_url, clean_text, page_access_state
from app.serialization import row

TOPIC_WORDS = {
    "劳动关系": ["劳动合同", "劳动关系", "劳动争议", "劳动法", "仲裁", "用工"],
    "社保与福利": ["社会保险", "社保", "养老保险", "医疗保险", "公积金", "生育保险"],
    "招聘与人才": ["招聘", "校招", "就业", "人才", "毕业生"],
    "薪酬": ["薪酬", "工资", "最低工资", "奖金"],
    "组织与绩效": ["绩效", "组织发展", "组织调整", "裁员"],
    "HR数字化": ["人力资源数字化", "人力资源管理系统", "人工智能", "AI招聘"],
}
DATE_PATTERN = r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?"
SAMPLE_NOTE = "仅包含配置来源本次实际读取的页面或Feed条目，不代表完整行业资讯。"


@dataclass
class NewsDocument:
    url: str
    title: str
    content: str
    published_at: datetime | None = None
    published_date: date | None = None
    publisher: str | None = None
    content_extent: str = "full_text"


@dataclass
class NewsCollection:
    documents: list[NewsDocument] = field(default_factory=list)
    pages: int = 0
    recognized: bool = False
    errors: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=lambda: [SAMPLE_NOTE])
    stopped_code: str | None = None


def retain_news_at_deadline(result, error):
    """Only elapsed budget may retain this attempt's readable partial sample."""
    if error.code != "time_budget" or not result.documents:
        raise error
    result.stopped_code = error.code
    result.recognized = True
    if not any(item["code"] == error.code for item in result.errors):
        result.errors.append({"code": error.code, "message": error.message})
        result.limitations.append("执行时间预算已达到，仅保留已读取文章；其余文章和来源尚未读取。")
    return result


def publication_date(value):
    """Do not invent a time or timezone when only a local calendar date is known."""
    if not value:
        return None, None
    value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else None), parsed.date()
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        return (parsed if parsed.tzinfo else None), parsed.date()
    except (TypeError, ValueError):
        pass
    match = re.search(DATE_PATTERN, value)
    if match:
        try:
            return None, date(*map(int, match.groups()))
        except ValueError:
            pass
    return None, None


def _policy_dates(content):
    dates = {}
    for name, pattern in {
        "effective_date": r"(?:自|于)\s*" + DATE_PATTERN + r"\s*(?:起)?\s*(?:施行|实施|生效|执行)",
        "policy_issued_date": r"(?:成文日期|印发日期|签发日期)\s*[:：]?\s*" + DATE_PATTERN,
    }.items():
        found = []
        for match in re.finditer(pattern, content):
            try:
                found.append(date(*map(int, match.groups())).isoformat())
            except ValueError:
                continue
        values = sorted(set(found))
        dates[name] = date.fromisoformat(values[0]) if len(values) == 1 else None
        dates[name + "_candidates"] = values
    return dates


def classify_article(document, source):
    body = document.title + " " + document.content
    topics = list(dict.fromkeys([*source.get("topics", []), *[
        name for name, words in TOPIC_WORDS.items() if any(word in body for word in words)]]))
    declared = source.get("source_type", "other")
    interpretation = bool(re.search(r"解读|答记者问|一图读懂|问答", document.title))
    policy = bool(re.search(r"关于.*(?:通知|办法|规定|意见)|条例|实施办法|实施细则|^中华人民共和国.{0,30}法(?:[（(].*)?$", document.title))
    if interpretation:
        content_type = "official_interpretation" if declared == "official" else "media_interpretation"
    elif policy and declared == "official":
        content_type = "policy_document"
    else:
        content_type = "official_news" if declared == "official" else "industry_news" if declared == "industry" else "source_article"
    dates = _policy_dates(document.content)
    if content_type != "policy_document":
        # A media article mentioning a date is not the policy's effective-date authority.
        dates["effective_date"], dates["policy_issued_date"] = None, None
    metadata = {"method": "rules_and_extract_v1", "source_type_basis": "administrator_configuration",
                "regions_basis": "source_configuration", "industries_basis": "source_configuration",
                "summary_type": "verbatim_excerpt", "content_extent": document.content_extent,
                "legal_conclusion": False, "date_candidates": {
                    key: dates[key] for key in ("effective_date_candidates", "policy_issued_date_candidates")},
                "uncertainty": []}
    audiences = sorted(set(re.findall(r"(?:适用对象|适用范围)\s*[:：]\s*([^。\n]{2,250})", document.content)))
    metadata["applicable_audience"] = audiences[0].strip() if content_type == "policy_document" and len(audiences) == 1 else None
    metadata["applicable_audience_basis"] = "explicit_source_text" if metadata["applicable_audience"] else "unknown"
    if not document.published_date:
        metadata["uncertainty"].append("来源未提供可确认的发布日期；采集日期不能代替发布日期")
    if content_type == "policy_document" and not dates["effective_date"]:
        metadata["uncertainty"].append("未找到唯一明确生效日期，请查看官方原文")
    if document.content_extent != "full_text":
        metadata["uncertainty"].append("当前只保留来源摘要，尚未验证全文")
    return content_type, topics, dates, metadata


def extract_article(html, url):
    soup = BeautifulSoup(html, "html.parser")
    state, message = page_access_state(html, url)
    if state:
        raise CollectionError(state, message)
    title_node = soup.select_one("h1, .pages-title, .article-title")
    title = clean_text(title_node.get_text(" ", strip=True) if title_node else soup.title.get_text(" ", strip=True) if soup.title else "")
    article = soup.select_one("article, [itemprop='articleBody'], #UCAP-CONTENT, #zoom, #zoomcon, #js_content, .TRS_Editor, .pages_content, .article-content")
    date_value, publisher = None, None
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if name in {"article:published_time", "pubdate", "publishdate", "firstpublishedtime", "date", "dc.date.issued"}:
            date_value = meta.get("content") or date_value
        elif name in {"contentsource", "source", "publisher", "dc.publisher"}:
            publisher = clean_text(meta.get("content"))[:200] or publisher
    time_element = soup.select_one("time[datetime]")
    if time_element:
        date_value = time_element.get("datetime") or date_value
    body, extent = "", "full_text"
    if article:
        for node in article.select("script, style, nav, header, footer, form"):
            node.decompose()
        body = clean_text(article.get_text(" ", strip=True))
    # Structured full article text is common on publisher sites.
    for script in soup.select("script[type='application/ld+json']")[:20]:
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        values = value if isinstance(value, list) else value.get("@graph", [value]) if isinstance(value, dict) else []
        for part in values:
            if not isinstance(part, dict) or not part.get("articleBody"):
                continue
            body = body or clean_text(part["articleBody"])
            title = title or clean_text(part.get("headline"))
            date_value = date_value or part.get("datePublished")
            if isinstance(part.get("publisher"), dict):
                publisher = publisher or clean_text(part["publisher"].get("name"))[:200]
    if not body or len(body) < 30 or not title:
        raise CollectionError("no_extractable_article", "页面没有可识别的资讯正文；未把栏目或导航当成文章")
    timestamp, calendar_date = publication_date(date_value)
    return NewsDocument(canonical_url(url), title[:600], body[:150000], timestamp, calendar_date, publisher, extent)


def extract_feed(content, url, max_items):
    if "<!DOCTYPE" in content.upper() or "<!ENTITY" in content.upper():
        raise CollectionError("unsafe_feed", "Feed包含不支持的外部实体或DTD")
    feed = feedparser.parse(content)
    if not feed.get("version"):
        raise CollectionError("invalid_feed", "来源没有返回可识别的RSS或Atom Feed")
    docs = []
    for entry in feed.entries[:max_items]:
        link = entry.get("link")
        if not link:
            continue
        try:
            link = canonical_url(urljoin(url, link))
        except ValueError:
            continue
        title = clean_text(entry.get("title"))[:600]
        full = entry.get("content") or []
        body = clean_text(full[0].get("value")) if full else clean_text(entry.get("summary"))
        if not title or not body:
            continue
        stamp, day = publication_date(entry.get("published"))
        docs.append(NewsDocument(link, title, body[:150000], stamp, day,
                                 clean_text((entry.get("source") or {}).get("title"))[:200] or None,
                                 "feed_content" if full else "feed_summary"))
    if feed.entries and not docs:
        raise CollectionError("no_extractable_article", "Feed有条目但未提供可识别标题、链接和正文或摘要")
    return docs, len(feed.entries)


def collect_news_source(source, max_articles=10, max_pages=3, fetcher=None, check=None):
    fetcher = fetcher or PublicFetcher()
    result = NewsCollection()
    try:
        if check:
            check()
        final, content = fetcher.fetch(source["url"])
        result.errors.extend({"url": final, **warning} for warning in getattr(fetcher, "render_warnings", []))
        result.pages += 1
        if source["kind"] == "feed":
            result.documents, total = extract_feed(content, final, max_articles)
            result.recognized = True
            result.limitations.append("Feed条目提供的正文或摘要；默认不追加逐条全文请求。")
            if total > max_articles:
                result.limitations.append("Feed条目超过本次预算，未读取全部条目。")
            return result
        try:
            result.documents = [extract_article(content, final)]
            result.recognized = True
            return result
        except CollectionError as exc:
            if exc.code != "no_extractable_article":
                raise
        # A configured section may expose same-host article links. Bound traversal to one level.
        soup = BeautifulSoup(content, "html.parser")
        links = []
        host = urlparse(final).hostname
        for anchor in soup.select("a[href]"):
            href = urljoin(final, anchor.get("href", ""))
            label = clean_text(anchor.get_text(" ", strip=True))
            if len(label) < 8 or urlparse(href).hostname != host:
                continue
            if not re.search(r"(?:\.s?html?(?:[?#]|$)|/\d{4}[-/]?\d{2}|/articles?/|/news/)", href):
                continue
            try:
                href = canonical_url(href)
            except ValueError:
                continue
            if href != canonical_url(final) and href not in links:
                links.append(href)
        for link in links[:min(max_articles, max(0, max_pages - 1))]:
            if check:
                check()
            try:
                current, html = fetcher.fetch(link)
                result.errors.extend({"url": current, **warning} for warning in getattr(fetcher, "render_warnings", []))
                result.pages += 1
                result.documents.append(extract_article(html, current))
            except CollectionError as exc:
                if exc.code in {"time_budget", "cancelled", "execution_superseded"}:
                    raise
                result.errors.append({"url": link, "code": exc.code, "message": exc.message})
            except httpx.RequestError:
                result.errors.append({"url": link, "code": "network_error", "message": "文章网络读取失败"})
        result.recognized = bool(result.documents)
        result.limitations.append("栏目页仅跟随同域一层文章链接，未遍历完整栏目。")
        if len(links) > max_pages - 1:
            result.limitations.append("栏目文章超过页面预算，尚有链接未读取。")
        if not result.recognized:
            raise CollectionError("no_extractable_article", "栏目未读取到可识别文章，不能认定没有新资讯")
        return result
    except CollectionError as exc:
        return retain_news_at_deadline(result, exc)
    finally:
        fetcher.close()


def ingest_news(db, task, source_run, documents):
    source = source_run.config_snapshot
    counts = Counter(received=len(documents), added=0, updated=0, unchanged=0, merged_content=0,
                     excluded=0, unmatched=0, unknown_publication_date=0)
    lock_notification_state(db)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "news-ingest"})
    for document in documents:
        body = document.title + " " + document.content
        if any(word.casefold() in body.casefold() for word in source.get("exclusions", [])):
            counts["excluded"] += 1
            continue
        if source.get("keywords") and not any(word.casefold() in body.casefold() for word in source["keywords"]):
            counts["unmatched"] += 1
            continue
        url = canonical_url(document.url)
        url_hash = hashlib.sha256(url.encode()).hexdigest()
        content_hash = hashlib.sha256((document.title.strip() + "\n" + document.content.strip()).encode()).hexdigest()
        item = db.scalar(select(NewsItem).where(NewsItem.url_hash == url_hash))
        if item is None:
            original = db.scalar(select(NewsObservation).where(NewsObservation.url_hash == url_hash)
                                 .order_by(NewsObservation.created_at.desc()).limit(1))
            if original:
                item = db.get(NewsItem, original.item_id)
                if item.url_hash != url_hash and item.content_hash != content_hash:
                    # An exact repost that later diverges becomes its own article.
                    item = None
        merged = False
        if item is None and len(document.content) >= 120:
            item = db.scalar(select(NewsItem).where(NewsItem.content_hash == content_hash).limit(1))
            merged = item is not None
        new = item is None
        if new:
            item = NewsItem(url_hash=url_hash, url=url, title=document.title,
                            content=document.content, content_hash=content_hash, excerpt=document.content[:500])
            db.add(item)
            counts["added"] += 1
        elif merged:
            counts["merged_content"] += 1
        else:
            counts["unchanged" if item.content_hash == content_hash else "updated"] += 1
        content_type, topics, dates, classification = classify_article(document, source)
        # Keep official provenance as primary when an exact copy exists on a media source.
        replace_primary = new or item.url_hash == url_hash or (
            source.get("source_type") == "official" and item.source_type != "official")
        if replace_primary:
            item.url, item.url_hash = url, url_hash
            item.title, item.content = document.title, document.content
            item.content_hash, item.excerpt = content_hash, document.content[:500]
            item.source_type, item.content_type = source.get("source_type", "other"), content_type
            item.publisher = document.publisher or source.get("publisher")
            item.published_at, item.published_date = document.published_at, document.published_date
            item.effective_date, item.policy_issued_date = dates["effective_date"], dates["policy_issued_date"]
            item.applicable_audience = classification["applicable_audience"]
            item.topics, item.regions = topics, source.get("regions", [])
            item.industries, item.classification = source.get("industries", []), classification
        item.last_seen_at = now()
        if document.published_date is None:
            counts["unknown_publication_date"] += 1
        db.flush()
        snapshot = {"url": url, "title": document.title, "content": document.content,
                    "publisher": document.publisher or source.get("publisher"),
                    "published_at": document.published_at.isoformat() if document.published_at else None,
                    "published_date": document.published_date.isoformat() if document.published_date else None,
                    "source_type": source.get("source_type", "other"), "classification": classification}
        if not db.scalar(select(NewsObservation.id).where(NewsObservation.item_id == item.id,
                          NewsObservation.source_run_id == source_run.id, NewsObservation.url_hash == url_hash)):
            encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
            digest = hashlib.sha256(encoded).hexdigest()
            folder = get_settings().storage_dir / "news" / str(task.id)
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = folder / (digest + ".json")
            path.write_bytes(encoded)
            path.chmod(0o600)
            raw = RawDocument(task_id=task.id, url=url, sha256=digest, storage_ref=str(path), content_type="application/json")
            db.add(raw)
            db.flush()
            db.add(NewsObservation(item_id=item.id, source_run_id=source_run.id, raw_document_id=raw.id,
                                   url=url, url_hash=url_hash, content_hash=content_hash, snapshot=snapshot))
        from app.news_policy_reminders import news_metadata, sync_policy_reminder
        if replace_primary:
            sync_policy_reminder(db, item, source_run)
        if new:
            emit_event(db, event_type="news.policy" if content_type == "policy_document" else "news.new",
                       dedupe_key="news:item:" + str(item.id), title=document.title,
                       text=item.excerpt + "\n来源摘录，请以原文为准。",
                       subject_id=uuid.UUID(source["subject_id"]) if source.get("subject_id") else None,
                       task_id=task.id, url=url, news=news_metadata(item))
    source_run.counts = dict(counts)
    return dict(counts)


def create_news_task(db, source_ids=None, *, max_articles=10, max_pages=3, render=False):
    query = select(NewsSource).where(NewsSource.enabled.is_(True))
    if source_ids:
        query = query.where(NewsSource.id.in_(source_ids))
    sources = db.scalars(query.order_by(NewsSource.created_at)).all()
    if source_ids and {source.id for source in sources} != set(source_ids):
        raise CollectionError("invalid_sources", "包含不存在或已停用的资讯来源")
    if not sources:
        raise CollectionError("no_sources", "请先配置至少一个可信资讯来源")
    if len(sources) > 20:
        raise CollectionError("source_budget", "一次任务最多20个来源，请缩小范围")
    task = TaskRun(domain="news", kind="news_collection", config_snapshot={
        "sources": [row(source) for source in sources], "max_articles": min(max(1, max_articles), 30),
        "max_pages": min(max(1, max_pages), 5), "render": render})
    db.add(task)
    db.flush()
    db.add(TaskEvent(task_id=task.id, message="资讯任务已创建；来源与筛选规则已保存"))
    db.commit()
    return task


def execute_news_task(task_id, *, fetcher_factory=None):
    identifier = uuid.UUID(str(task_id))
    with session_factory()() as db:
        claimed = db.execute(update(TaskRun).where(TaskRun.id == identifier, TaskRun.domain == "news",
                            TaskRun.kind == "news_collection", TaskRun.status == "queued",
                            TaskRun.cancel_requested.is_(False)).values(status="running", started_at=now(),
                            heartbeat_at=now(), attempt=TaskRun.attempt + 1).returning(TaskRun.id)).first()
        db.commit()
        if not claimed:
            return
        task = db.get(TaskRun, identifier)
        execution_attempt = task.attempt
        execution_started_at = task.started_at
        config = task.config_snapshot
        try:
            for index, frozen in enumerate(config["sources"]):
                def check(*, allow_expired=False, for_commit=False):
                    # The final check and ingest must share one transaction: a
                    # committed cancel/new owner cannot slip between them.
                    db.refresh(task, with_for_update=for_commit)
                    if task.attempt != execution_attempt or task.status != "running":
                        raise CollectionError("execution_superseded", "任务执行权已变化，旧工作进程停止")
                    if task.cancel_requested:
                        raise CollectionError("cancelled", "资讯任务已取消")
                    if not allow_expired and (now() - execution_started_at).total_seconds() > get_settings().task_timeout_seconds:
                        raise CollectionError("time_budget", "资讯采集达到执行时间预算")
                    task.heartbeat_at = now()
                    if not for_commit:
                        db.commit()
                check()
                source = db.get(NewsSource, uuid.UUID(frozen["id"]))
                if not source or not source.enabled:
                    raise CollectionError("source_changed", "资讯来源已停用，请重新创建任务")
                run = db.scalar(select(NewsSourceRun).where(NewsSourceRun.task_id == task.id,
                                                           NewsSourceRun.source_id == source.id))
                if run and run.status in {"succeeded", "partial"}:
                    continue
                if run is None:
                    run = NewsSourceRun(task_id=task.id, source_id=source.id, config_snapshot=frozen)
                    db.add(run)
                run.status, run.error_code, run.error_message = "running", None, None
                db.commit()
                halt = False
                try:
                    if fetcher_factory:
                        fetcher = fetcher_factory(frozen)
                    elif config.get("render") and frozen["kind"] == "web":
                        from app.sentiment_service import SentimentBrowserFetcher
                        fetcher = SentimentBrowserFetcher(platform="news")
                    else:
                        fetcher = None
                    result = collect_news_source(frozen, config["max_articles"], config["max_pages"], fetcher, check)
                    try:
                        check()
                    except CollectionError as exc:
                        retain_news_at_deadline(result, exc)
                    halt = result.stopped_code == "time_budget"
                    # Expired time does not relax cancellation or attempt ownership.
                    check(allow_expired=halt, for_commit=True)
                    counts = ingest_news(db, task, run, result.documents)
                    run.status = "partial" if result.errors else "succeeded"
                    run.coverage = "feed_sample" if frozen["kind"] == "feed" else "page_sample"
                    run.counts = {**counts, "pages": result.pages, "page_errors": result.errors}
                    run.limitations = result.limitations
                    if result.errors:
                        run.error_code, run.error_message = result.errors[0]["code"], result.errors[0]["message"]
                    source.last_success_at = now()
                except CollectionError as exc:
                    if exc.code == "execution_superseded":
                        db.rollback()
                        return "superseded"
                    run.status = exc.code if exc.code in {"cancelled", "waiting_login", "waiting_verification"} else "failed"
                    run.error_code, run.error_message = exc.code, exc.message
                    run.limitations = [SAMPLE_NOTE]
                    if exc.code in {"cancelled", "time_budget"}:
                        # Keep any final-check lock until the task and source
                        # cancellation/failure state commit together below.
                        raise
                except httpx.RequestError:
                    run.status, run.error_code = "failed", "network_error"
                    run.error_message = "资讯来源网络读取失败，未判定为没有新资讯"
                except Exception:
                    db.rollback()
                    task = db.get(TaskRun, identifier)
                    source = db.get(NewsSource, source.id)
                    run = db.get(NewsSourceRun, run.id)
                    run.status, run.error_code = "failed", "source_runtime_error"
                    run.error_message = "资讯来源处理异常，未判定为没有新资讯"
                source.health, run.finished_at = run.status, now()
                task.progress = int((index + 1) / len(config["sources"]) * 95)
                db.add(TaskEvent(task_id=task.id, message=f"{source.name}：{run.status}",
                                 level="info" if run.status == "succeeded" else "warning",
                                 detail={"source_run_id": str(run.id), "error_code": run.error_code}))
                if halt:
                    task.error_code, task.error_message = "time_budget", "资讯采集达到时间预算，已保留已读文章"
                    # Retain the row lock through the task's terminal state too;
                    # a source-only commit would reopen the ownership window.
                    break
                db.commit()
            runs = db.scalars(select(NewsSourceRun).where(NewsSourceRun.task_id == identifier)).all()
            valid = [run for run in runs if run.status in {"succeeded", "partial"}]
            task.status = "succeeded" if runs and all(run.status == "succeeded" for run in runs) else "partial" if valid else "failed"
            task.summary = {"sources": len(runs), "valid_sources": len(valid),
                            "requested_sources": len(config["sources"]),
                            "unattempted_sources": len(config["sources"]) - len(runs),
                            "added": sum(run.counts.get("added", 0) for run in runs),
                            "coverage": "source_sample" if valid else "unknown"}
        except CollectionError as exc:
            if exc.code == "execution_superseded":
                db.rollback()
                return "superseded"
            task.status = "cancelled" if exc.code == "cancelled" else "failed"
            task.error_code, task.error_message = exc.code, exc.message
            if exc.code == "time_budget":
                runs = db.scalars(select(NewsSourceRun).where(NewsSourceRun.task_id == identifier)).all()
                valid = [run for run in runs if run.status in {"succeeded", "partial"}]
                task.status = "partial" if valid else "failed"
                task.summary = {"sources": len(runs), "valid_sources": len(valid),
                                "requested_sources": len(config["sources"]),
                                "unattempted_sources": len(config["sources"]) - len(runs),
                                "added": sum(run.counts.get("added", 0) for run in runs),
                                "coverage": "source_sample" if valid else "unknown"}
        except Exception:
            db.rollback()
            task = db.get(TaskRun, identifier)
            task.status, task.error_code = "failed", "news_runtime_error"
            task.error_message = "资讯任务执行失败，已提交的来源记录仍保留"
        for unfinished in db.scalars(select(NewsSourceRun).where(
            NewsSourceRun.task_id == identifier, NewsSourceRun.status == "running")):
            unfinished.status = "cancelled" if task.status == "cancelled" else "failed"
            unfinished.error_code = task.error_code or "task_interrupted"
            unfinished.error_message = task.error_message or "任务未完成此来源"
            unfinished.finished_at = now()
        task.finished_at, task.heartbeat_at, task.progress = now(), now(), 100
        db.commit()
        return task.status
