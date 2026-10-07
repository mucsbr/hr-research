"""Bounded retention: preserve business rows, never traverse an untrusted filesystem path."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import delete, exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_engine, session_factory
from app.models import AuditEvent, BrowserLoginSession, RawDocument, Report, SourceRun, TaskEvent, TaskRun, UserSession, now
from app.retention_models import RetentionPolicy, RetentionPreview, RetentionRun
from app.serialization import row

DEFAULTS = {"raw_documents": 90, "reports": 180, "task_logs": 90, "expired_sessions": 7}
TERMINAL_TASKS = {"succeeded", "failed", "cancelled", "partial"}
MAX_BATCH = 200
RUN_SECONDS = 30
MAX_FILES = 100
MAX_FILE_BYTES = 32_000_000
GRACE_DAYS = 7
PREVIEW_SECONDS = 900
PROTECTED = ["岗位、趋势、资讯与舆情业务记录及来源哈希", "模型预算、通知记录与规则", "适配器代码包、候选、检查点及账号Profile",
             "活动任务与未过期会话", "BettaFish缓存及未登记文件/目录"]


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def ensure_policies(db, at=None):
    at = at or now()
    for category, days in DEFAULTS.items():
        db.execute(insert(RetentionPolicy).values(category=category, retention_days=days, enabled=True,
            revision=1, grace_until=at + timedelta(days=GRACE_DAYS)).on_conflict_do_nothing(index_elements=["category"]))
    db.flush()


def policies_response(db, *, can_manage):
    ensure_policies(db)
    db.commit()
    policies = db.scalars(select(RetentionPolicy).order_by(RetentionPolicy.category)).all()
    return {"policies": [row(policy) for policy in policies], "can_manage": can_manage,
            "limits": {"retention_days_min": 1, "retention_days_max": 3650, "batch_size_max": MAX_BATCH,
                       "preview_ttl_seconds": PREVIEW_SECONDS},
            "scheduler": {"enabled": any(policy.enabled for policy in policies), "batch_size": 100, "max_seconds": RUN_SECONDS},
            "protected": PROTECTED}


def change_policies(db, updates, actor_id, *, at=None):
    at = at or now()
    ensure_policies(db, at)
    policies = {policy.category: policy for policy in db.scalars(select(RetentionPolicy)
                .order_by(RetentionPolicy.category).with_for_update())}
    for update in updates:
        policy = policies[update["category"]]
        if policy.revision != update["expected_revision"]:
            raise HTTPException(409, "保留策略已被其他操作更新，请刷新后重新预览")
    for update in updates:
        policy = policies[update["category"]]
        if policy.retention_days == update["retention_days"] and policy.enabled == update["enabled"]:
            continue
        if update["retention_days"] < policy.retention_days or update["enabled"] and not policy.enabled:
            policy.grace_until = max(policy.grace_until, at + timedelta(days=GRACE_DAYS))
        policy.retention_days, policy.enabled = update["retention_days"], update["enabled"]
        policy.revision += 1
        db.add(AuditEvent(user_id=actor_id, action="retention.policy.updated", target=policy.category,
                         detail={"retention_days": policy.retention_days, "enabled": policy.enabled,
                                 "revision": policy.revision, "grace_until": stamp(policy.grace_until)}))
    db.commit()


def task_available(db, task_id, *, lock=False):
    if task_id is None:
        return True
    query = select(TaskRun).where(TaskRun.id == task_id)
    task = db.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    if task is None or task.status not in TERMINAL_TASKS:
        return False
    from app.news_models import NewsSourceRun
    from app.sentiment_models import SentimentSourceRun
    for source_model in (SourceRun, NewsSourceRun, SentimentSourceRun):
        pending = db.scalar(select(source_model.id).where(source_model.task_id == task_id,
            source_model.status.in_(["queued", "running", "waiting_login", "waiting_verification", "waiting_account"])).limit(1))
        if pending:
            return False
    return True


def due_query(category, cutoff, at):
    from app.news_models import NewsSourceRun
    from app.sentiment_models import SentimentSourceRun
    terminal = select(TaskRun.id).where(TaskRun.status.in_(TERMINAL_TASKS))
    for source_model in (SourceRun, NewsSourceRun, SentimentSourceRun):
        terminal = terminal.where(~exists(select(source_model.id).where(source_model.task_id == TaskRun.id,
            source_model.status.in_(["queued", "running", "waiting_login", "waiting_verification", "waiting_account"]))))
    if category == "raw_documents":
        return select(RawDocument).where(RawDocument.created_at < cutoff, RawDocument.task_id.in_(terminal),
            or_(RawDocument.cleared_at.is_(None), RawDocument.retention["files_pending"].as_boolean().is_(True)),
            or_(RawDocument.retention["retry_after"].astext.is_(None), RawDocument.retention["retry_after"].astext <= stamp(at)))
    if category == "reports":
        return select(Report).where(Report.created_at < cutoff, or_(Report.task_id.is_(None), Report.task_id.in_(terminal)),
            or_(Report.cleared_at.is_(None), Report.retention["files_pending"].as_boolean().is_(True)),
            or_(Report.retention["retry_after"].astext.is_(None), Report.retention["retry_after"].astext <= stamp(at)))
    if category == "task_logs":
        old_events = exists(select(TaskEvent.id).where(TaskEvent.task_id == TaskRun.id, TaskEvent.created_at < cutoff))
        return select(TaskRun).where(TaskRun.id.in_(terminal), TaskRun.finished_at < cutoff,
            or_(TaskRun.logs_cleared_at.is_(None), old_events, TaskRun.log_retention["files_pending"].as_boolean().is_(True)),
            or_(TaskRun.log_retention["retry_after"].astext.is_(None), TaskRun.log_retention["retry_after"].astext <= stamp(at)))
    return None


def create_preview(db, *, categories=None, limit=100, actor_id=None, at=None):
    at = at or now()
    if not 1 <= limit <= MAX_BATCH:
        raise HTTPException(422, "单次清理预算必须为1至200")
    selected = list(dict.fromkeys(categories or DEFAULTS))
    if set(selected) - set(DEFAULTS):
        raise HTTPException(422, "保留分类不存在")
    ensure_policies(db, at)
    policies = db.scalars(select(RetentionPolicy).where(RetentionPolicy.category.in_(selected))
                         .order_by(RetentionPolicy.category).with_for_update()).all()
    items, snapshots, groups = [], {}, []
    for policy in policies:
        cutoff = at - timedelta(days=policy.retention_days)
        state = "disabled" if not policy.enabled else "grace_period" if at < policy.grace_until else "ready"
        snapshots[policy.category] = {"revision": policy.revision, "retention_days": policy.retention_days,
                                     "cutoff_at": stamp(cutoff), "grace_until": stamp(policy.grace_until)}
        group = {"category": policy.category, "retention_days": policy.retention_days, "cutoff_at": stamp(cutoff),
                 "grace_until": stamp(policy.grace_until), "state": state, "eligible_count": 0, "has_more": False}
        if state == "ready":
            remaining = limit - len(items)
            if policy.category == "expired_sessions":
                candidates = [(kind, record) for kind, model in [("user_session", UserSession), ("browser_session", BrowserLoginSession)]
                              for record in db.scalars(select(model).where(model.expires_at < cutoff)
                                  .order_by(model.expires_at, model.id).limit(remaining + 1))]
                candidates.sort(key=lambda pair: (pair[1].expires_at, str(pair[1].id)))
            else:
                query = due_query(policy.category, cutoff, at)
                model = {"raw_documents": RawDocument, "reports": Report, "task_logs": TaskRun}[policy.category]
                # Bounded over-scan lets active tasks be skipped without treating them as deletable.
                rows = db.scalars(query.order_by(model.created_at, model.id).limit(MAX_BATCH + 1)).all()
                candidates = [(policy.category, record) for record in rows if task_available(db,
                    record.id if policy.category == "task_logs" else record.task_id)]
            group["has_more"] = len(candidates) > remaining
            for kind, record in candidates[:remaining]:
                task_id = record.id if policy.category == "task_logs" else getattr(record, "task_id", None)
                item = {"category": policy.category, "object_type": kind, "target_id": str(record.id),
                        "task_id": str(task_id) if task_id else None, "status": "pending"}
                if isinstance(record, RawDocument):
                    item.update(url=record.url, expected_sha=record.sha256, storage_ref=record.storage_ref)
                if isinstance(record, Report):
                    item["title"] = record.title
                items.append(item)
                group["eligible_count"] += 1
        groups.append(group)
    estimated_bytes, measured = 0, set()
    deadline = time.monotonic() + RUN_SECONDS
    models = {"raw_documents": RawDocument, "reports": Report, "task_logs": TaskRun,
              "user_session": UserSession, "browser_session": BrowserLoginSession}
    for item in items:
        obj = db.get(models[item["object_type"]], uuid.UUID(item["target_id"]))
        try:
            for entry in object_files(obj, item["category"]):
                if entry.get("deferred") or time.monotonic() >= deadline:
                    item["file_state"] = "bounded_estimate"
                    break
                if entry["ref"] in measured:
                    continue
                measured.add(entry["ref"])
                if shared_reference(db, entry["ref"], raw_id=obj.id if isinstance(obj, RawDocument) else None,
                                    report_id=obj.id if isinstance(obj, Report) else None,
                                    task_id=getattr(obj, "task_id", None)):
                    item["file_state"] = "shared_reference"
                    continue
                info = inspect_file(entry["ref"], expected_sha=entry.get("expected_sha"), deadline=deadline)
                estimated_bytes += info["bytes"]
        except FileSafetyError as exc:
            item["file_state"] = exc.code
    preview = RetentionPreview(actor_id=actor_id, expires_at=at + timedelta(seconds=PREVIEW_SECONDS),
        policy_snapshot=snapshots, items=items,
        summary={"can_execute": bool(items), "eligible_count": len(items), "estimated_bytes": estimated_bytes, "categories": groups,
                 "limitations": ["计数仅为本次有界批次，不是全库总量；活动任务已排除。", "文件安全、共享引用及策略有效性在执行前重新核验；预计字节数不包含尚未打开的文件。", *PROTECTED]})
    db.add(preview)
    db.commit()
    return preview


def public_items(items):
    allowed = {"category", "target_id", "task_id", "title", "url", "file_state", "status", "reason", "files_deleted", "bytes_deleted", "events_deleted"}
    return [{key: value for key, value in item.items() if key in allowed} for item in items]


def preview_response(preview):
    return {"id": str(preview.id), "created_at": stamp(preview.created_at), "expires_at": stamp(preview.expires_at),
            **preview.summary, "items": public_items(preview.items)}


def run_response(run):
    return {**row(run, exclude={"items", "policy_snapshot", "actor_id"}), "items": public_items(run.items)}


def create_run(db, preview_id, idempotency_key, *, actor_id=None, trigger="manual", at=None):
    at = at or now()
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "retention-run:" + idempotency_key})
    existing = db.scalar(select(RetentionRun).where(RetentionRun.idempotency_key == idempotency_key))
    if existing:
        if existing.preview_id != preview_id or existing.actor_id != actor_id or existing.trigger != trigger:
            raise HTTPException(409, "幂等键已用于其他清理请求")
        db.commit()
        return existing, False
    preview = db.scalar(select(RetentionPreview).where(RetentionPreview.id == preview_id).with_for_update())
    if preview is None or preview.actor_id != actor_id:
        raise HTTPException(404, "清理预览不存在或不属于当前用户")
    if preview.expires_at <= at:
        raise HTTPException(409, "清理预览已过期，请重新预览")
    if not preview.items:
        raise HTTPException(409, "本次预览没有可执行记录；停用或宽限内不会清理")
    if db.scalar(select(RetentionRun.id).where(RetentionRun.preview_id == preview_id)):
        raise HTTPException(409, "该预览已用于其他运行，请查询原记录")
    policies = db.scalars(select(RetentionPolicy).where(RetentionPolicy.category.in_(preview.policy_snapshot))
                         .order_by(RetentionPolicy.category).with_for_update()).all()
    if len(policies) != len(preview.policy_snapshot) or any(policy.revision != preview.policy_snapshot[policy.category]["revision"] for policy in policies):
        raise HTTPException(409, "策略已变化，请重新预览")
    if any(not policy.enabled or policy.grace_until > at for policy in policies if any(item["category"] == policy.category for item in preview.items)):
        raise HTTPException(409, "策略已停用或仍在宽限期")
    run = RetentionRun(preview_id=preview.id, actor_id=actor_id, idempotency_key=idempotency_key, trigger=trigger,
        policy_snapshot=deepcopy(preview.policy_snapshot), items=deepcopy(preview.items),
        summary={"selected": len(preview.items), "cleared": 0, "skipped": 0, "failed": 0, "files_deleted": 0, "bytes_deleted": 0})
    db.add(run)
    db.flush()
    db.add(AuditEvent(user_id=actor_id, action="retention.run.queued", target=str(run.id), detail={"trigger": trigger, "selected": len(run.items)}))
    db.commit()
    return run, True


class FileSafetyError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def relative_ref(reference):
    configured = Path(get_settings().storage_dir)
    root = Path(os.path.abspath(configured))
    target = Path(reference)
    if ".." in target.parts or not reference or "\x00" in reference:
        raise FileSafetyError("unsafe_path")
    if target.is_absolute():
        try:
            target = target.relative_to(root)
        except ValueError:
            raise FileSafetyError("outside_storage") from None
    elif not configured.is_absolute() and target.parts[:len(configured.parts)] == configured.parts:
        # Older writers may serialize a cwd-relative configured storage directory.
        target = Path(*target.parts[len(configured.parts):])
    if not target.parts or any(part in {"", ".", ".."} for part in target.parts):
        raise FileSafetyError("unsafe_path")
    return target.as_posix()


def parent_fd(reference):
    relative = relative_ref(reference)
    parts = Path(relative).parts
    descriptor = os.open(os.path.abspath(get_settings().storage_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def inspect_file(reference, *, expected_sha=None, remove=False, deadline=None):
    directory = handle = None
    try:
        directory, name = parent_fd(reference)
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise FileSafetyError("not_regular_or_symlink")
        if before.st_size > MAX_FILE_BYTES:
            raise FileSafetyError("file_budget")
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        initial = os.fstat(handle)
        if (initial.st_dev, initial.st_ino) != (before.st_dev, before.st_ino):
            raise FileSafetyError("file_changed")
        hasher = hashlib.sha256()
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise FileSafetyError("time_budget")
            chunk = os.read(handle, 1_000_000)
            if not chunk:
                break
            hasher.update(chunk)
        sha = hasher.hexdigest()
        final = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns):
            raise FileSafetyError("file_changed")
        if expected_sha and sha != expected_sha:
            raise FileSafetyError("hash_mismatch")
        if remove:
            os.unlink(name, dir_fd=directory)
        return {"state": "deleted" if remove else "present", "sha256": sha, "bytes": initial.st_size}
    except FileNotFoundError:
        return {"state": "missing", "bytes": 0}
    except OSError as exc:
        raise FileSafetyError("unsafe_or_unavailable_file") from exc
    finally:
        if handle is not None:
            os.close(handle)
        if directory is not None:
            os.close(directory)


def known_report_paths(task_id, *, logs_only=False):
    """Known fixed upstream layout only, including ChapterStorage's nested body copies.

    BettaFish ReportEngine/core/chapter_storage.py: chapters/report-<8hex>/
    manifest.json and <order>-<slug>/{chapter.json,stream.raw}; agent.py also
    writes flat reports and IR. Caches and unregistered names are never traversed.
    """
    prefix = "bettafish/" + str(task_id)
    paths = []
    fixed = [prefix + "/worker.log", prefix + "/logs/report.log"]
    if not logs_only:
        fixed += [prefix + "/" + name for name in ["request.json", "result.html", "upstream-result.html"]]
    for reference in fixed:
        descriptor = None
        try:
            descriptor, name = parent_fd(reference)
            os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            paths.append(reference)
        except FileNotFoundError:
            pass
        except OSError:
            paths.append(reference)
        finally:
            if descriptor is not None:
                os.close(descriptor)
    if logs_only:
        return paths, False
    scanned, truncated = 0, False

    def listing(relative):
        nonlocal scanned, truncated
        descriptor = None
        try:
            descriptor, _ = parent_fd(relative + "/__directory_anchor__")
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > 1000 or len(paths) > MAX_FILES:
                        truncated = True
                        break
                    yield entry.name
        except FileNotFoundError:
            return
        except OSError:
            # The synthetic leaf reports an inaccessible/symlinked known parent;
            # no unlink follows it, while other safe files can still be handled.
            paths.append(relative + "/__unavailable__")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    for folder, pattern in [("reports", r"(?:final_report_.+\.html|report_state_.+\.json)"),
                            ("ir", r"report_ir_.+\.json"), ("json-errors", r"[^/]+\.json")]:
        relative = prefix + "/" + folder
        for name in listing(relative):
            if re.fullmatch(pattern, name):
                paths.append(relative + "/" + name)
    for report_name in listing(prefix + "/chapters"):
        if not re.fullmatch(r"report-[0-9a-f]{8}", report_name):
            continue
        report_dir = prefix + "/chapters/" + report_name
        for name in listing(report_dir):
            if name == "manifest.json":
                paths.append(report_dir + "/manifest.json")
            elif re.fullmatch(r"[0-9]{3,}-[^/]{1,200}", name):
                chapter_dir = report_dir + "/" + name
                for artifact in listing(chapter_dir):
                    if artifact in {"chapter.json", "stream.raw"}:
                        paths.append(chapter_dir + "/" + artifact)
    return paths[:MAX_FILES], truncated or len(paths) > MAX_FILES


def shared_reference(db, reference, *, raw_id=None, report_id=None, task_id=None):
    from app.adapter_code_models import AdapterCodePackage
    from app.checkpoint_models import OfficialCheckpointStep
    from app.models import SourceAccount
    relative = relative_ref(reference)
    absolute = str(Path(os.path.abspath(get_settings().storage_dir)) / relative)
    aliases = [relative, absolute]
    if db.scalar(select(OfficialCheckpointStep.id).where(OfficialCheckpointStep.storage_ref.in_(aliases)).limit(1)):
        return True
    if db.scalar(select(AdapterCodePackage.id).where(AdapterCodePackage.file_ref.in_(aliases)).limit(1)):
        return True
    if db.scalar(select(SourceAccount.id).where(or_(SourceAccount.profile_ref.in_(aliases), SourceAccount.session_ref.in_(aliases))).limit(1)):
        return True
    # Normalize absolute/relative aliases and harmless './' variants, without resolving links.
    refs = db.scalars(select(RawDocument).where(RawDocument.id != raw_id if raw_id else True,
        RawDocument.cleared_at.is_(None), RawDocument.storage_ref.endswith("/" + Path(relative).name, autoescape=True)).limit(MAX_BATCH + 1)).all()
    if len(refs) > MAX_BATCH:
        return True
    for other in refs:
        try:
            if relative_ref(other.storage_ref) == relative:
                return True
        except FileSafetyError:
            continue
    if task_id and relative.startswith("bettafish/" + str(task_id) + "/"):
        query = select(Report.id).where(Report.task_id == task_id, Report.cleared_at.is_(None))
        if report_id:
            query = query.where(Report.id != report_id)
        if db.scalar(query.limit(1)):
            return True
    return False


def eligible_item(db, run, item, at, deadline=None):
    # Thread-pool workers cannot enforce Celery soft limits; database waits must also be bounded.
    milliseconds = max(1, min(3000, int((deadline - time.monotonic()) * 1000))) if deadline else 3000
    db.execute(text("SELECT set_config('lock_timeout', :value, true)"), {"value": str(min(milliseconds, 1000)) + "ms"})
    db.execute(text("SELECT set_config('statement_timeout', :value, true)"), {"value": str(milliseconds) + "ms"})
    policy = db.scalar(select(RetentionPolicy).where(RetentionPolicy.category == item["category"])
                       .with_for_update().execution_options(populate_existing=True))
    saved = run.policy_snapshot[item["category"]]
    if not policy or not policy.enabled or policy.revision != saved["revision"] or policy.grace_until > at:
        return None, "policy_changed_or_grace"
    cutoff = datetime.fromisoformat(saved["cutoff_at"])
    if item.get("task_id") and not task_available(db, uuid.UUID(item["task_id"]), lock=True):
        return None, "active_task"
    model = {"raw_documents": RawDocument, "reports": Report, "task_logs": TaskRun,
             "user_session": UserSession, "browser_session": BrowserLoginSession}[item["object_type"]]
    obj = db.scalar(select(model).where(model.id == uuid.UUID(item["target_id"]))
                    .with_for_update().execution_options(populate_existing=True))
    if obj is None:
        return None, "already_missing"
    if item["category"] in {"raw_documents", "reports", "task_logs"}:
        current_task = obj.id if isinstance(obj, TaskRun) else obj.task_id
        if (str(current_task) if current_task else None) != item.get("task_id"):
            return None, "reference_changed"
    when = obj.expires_at if item["category"] == "expired_sessions" else obj.finished_at if item["category"] == "task_logs" else obj.created_at
    if when is None or when >= cutoff:
        return None, "no_longer_due"
    if isinstance(obj, RawDocument) and (obj.storage_ref != item["storage_ref"] or obj.sha256 != item["expected_sha"]):
        return None, "reference_changed"
    if isinstance(obj, (RawDocument, Report)) and obj.cleared_at and not obj.retention.get("files_pending"):
        return None, "already_cleared"
    return obj, None


def object_files(obj, category):
    if category == "raw_documents":
        relative = relative_ref(obj.storage_ref)
        parts = Path(relative).parts
        if (len(parts) != 3 or parts[0] not in {"tasks", "sentiment", "news"}
                or parts[1] != str(obj.task_id) or Path(parts[2]).stem != obj.sha256
                or Path(parts[2]).suffix not in {".txt", ".json", ".html"}):
            raise FileSafetyError("unregistered_raw_path")
        return [{"ref": relative, "expected_sha": obj.sha256}]
    if category == "reports" and obj.task_id and obj.coverage.get("engine") == "bettafish":
        results = []
        paths, truncated = known_report_paths(obj.task_id)
        for reference in paths:
            sha = obj.coverage.get("artifact_sha256") if reference.endswith("/result.html") else obj.coverage.get("upstream_artifact_sha256") if reference.endswith("/upstream-result.html") else None
            results.append({"ref": reference, "expected_sha": sha})
        if truncated:
            results.append({"deferred": True})
        return results
    if category == "task_logs":
        return [{"ref": reference, "expected_sha": None} for reference in known_report_paths(obj.id, logs_only=True)[0]]
    return []


def file_intents(db, obj, item, deadline, remaining=MAX_FILES):
    intents = []
    paths = object_files(obj, item["category"])
    for entry in paths[:remaining]:
        if entry.get("deferred"):
            intents.append({"state": "deferred", "reason": "file_batch_limit"})
            continue
        try:
            shared = shared_reference(db, entry["ref"], raw_id=obj.id if isinstance(obj, RawDocument) else None,
                                      report_id=obj.id if isinstance(obj, Report) else None,
                                      task_id=getattr(obj, "task_id", None))
            if shared:
                intents.append({**entry, "state": "shared"})
                continue
            info = inspect_file(entry["ref"], expected_sha=entry["expected_sha"], deadline=deadline)
            intents.append({**entry, **info})
        except FileSafetyError as exc:
            intents.append({**entry, "state": "failed", "reason": exc.code})
    if len(paths) > remaining:
        intents.append({"state": "deferred", "reason": "file_batch_limit"})
    return intents


def clear_inline_report(report, at, retention):
    if not report.cleared_at:
        retention.update(content_sha256=hashlib.sha256(report.content.encode()).hexdigest(),
                         evidence_sha256=digest(report.evidence), coverage_sha256=digest(report.coverage))
        report.content = ""
        fields = {"job_id", "record_id", "item_id", "raw_document_id", "title", "source_url", "url", "last_seen_at"}
        report.evidence = [{key: value for key, value in item.items() if key in fields}
                           for item in report.evidence if isinstance(item, dict)]
        allowed = {"engine", "subject_id", "revision", "artifact_sha256", "upstream_artifact_sha256",
                   "model_call_ids", "model_call_id", "report_profile", "as_of", "days", "model_calls", "comparison_basis", "comparable"}
        report.coverage = {key: value for key, value in report.coverage.items() if key in allowed}
        report.cleared_at = at


def process_item(db, run, item, at, deadline):
    obj, reason = eligible_item(db, run, item, at, deadline)
    if obj is None:
        return {**item, "status": "skipped", "reason": reason}
    results, deleted, removed_bytes, failed = [], 0, 0, False
    for intent in item.get("file_intents", []):
        if intent["state"] == "deferred":
            results.append(intent)
            failed = True
            continue
        if time.monotonic() >= deadline:
            results.append({**intent, "state": "failed", "reason": "time_budget"})
            failed = True
            continue
        try:
            if shared_reference(db, intent["ref"], raw_id=obj.id if isinstance(obj, RawDocument) else None,
                                report_id=obj.id if isinstance(obj, Report) else None,
                                task_id=getattr(obj, "task_id", None)):
                results.append({**intent, "state": "shared"})
                continue
            if intent["state"] == "failed":
                failed = True
                results.append(intent)
                continue
            # The persisted pre-unlink digest also protects unhashed request/log artifacts.
            expected = intent.get("sha256") or intent.get("expected_sha")
            info = inspect_file(intent["ref"], expected_sha=expected, remove=True, deadline=deadline)
            results.append({**intent, **info})
            deleted += info["state"] == "deleted"
            removed_bytes += info["bytes"] if info["state"] == "deleted" else 0
        except FileSafetyError as exc:
            failed = True
            results.append({**intent, "state": "failed", "reason": exc.code})
    if isinstance(obj, (RawDocument, Report)):
        metadata = {**obj.retention, "run_id": str(run.id), "policy_revision": run.policy_snapshot[item["category"]]["revision"],
                    "attempted_at": stamp(at), "files": results, "files_pending": failed}
        if failed:
            metadata["retry_after"] = stamp(at + timedelta(days=1))
        else:
            metadata.pop("retry_after", None)
        if isinstance(obj, Report):
            clear_inline_report(obj, at, metadata)
        else:
            obj.cleared_at = obj.cleared_at or at
        obj.retention = metadata
    events_deleted = 0
    if item["category"] == "task_logs":
        cutoff = datetime.fromisoformat(run.policy_snapshot[item["category"]]["cutoff_at"])
        records = db.scalars(select(TaskEvent).where(TaskEvent.task_id == obj.id, TaskEvent.created_at < cutoff)
                             .order_by(TaskEvent.created_at, TaskEvent.id).limit(500)).all()
        hashes = [{"id": str(event.id), "sha256": digest({"message": event.message, "detail": event.detail})} for event in records]
        if records:
            events_deleted = db.execute(delete(TaskEvent).where(TaskEvent.id.in_([event.id for event in records]))).rowcount
        obj.logs_cleared_at = at
        obj.log_retention = {"run_id": str(run.id), "cleared_at": stamp(at), "events_deleted": events_deleted,
                             "events_sha256": digest(hashes), "files": results, "files_pending": failed,
                             **({"retry_after": stamp(at + timedelta(days=1))} if failed else {})}
    if item["category"] == "expired_sessions":
        db.delete(obj)
    return {**item, "status": "failed" if failed else "cleared", "reason": "file_cleanup_partial" if failed else "retention_applied",
            "file_intents": results, "files_deleted": deleted, "bytes_deleted": removed_bytes, "events_deleted": events_deleted}


def run_totals(items):
    return {"selected": len(items), **{state: sum(item["status"] == state for item in items) for state in ["cleared", "skipped", "failed"]},
            "files_deleted": sum(item.get("files_deleted", 0) for item in items),
            "bytes_deleted": sum(item.get("bytes_deleted", 0) for item in items)}


def execute_run(run_id, *, at=None):
    at = at or now()
    deadline = time.monotonic() + RUN_SECONDS
    with get_engine().connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(hashtextextended('retention-execution', 0))"))
        connection.commit()
        if not acquired:
            return {"id": str(run_id), "status": "queued", "reason": "another_retention_run_active"}
        try:
            with Session(bind=connection, expire_on_commit=False) as db:
                run = db.get(RetentionRun, uuid.UUID(str(run_id)))
                if run is None:
                    raise HTTPException(404, "清理运行不存在")
                if run.status not in {"queued", "running"}:
                    return run_response(run)
                run.status, run.started_at, run.heartbeat_at = "running", run.started_at or at, at
                db.commit()
                for index in range(len(run.items)):
                    if time.monotonic() >= deadline or run.summary.get("files_deleted", 0) >= MAX_FILES:
                        break
                    item = deepcopy(run.items[index])
                    if item["status"] not in {"pending", "deleting"}:
                        continue
                    outcome = None
                    try:
                        obj, reason = eligible_item(db, run, item, at, deadline)
                        if obj is None:
                            outcome = {**item, "status": "skipped", "reason": reason}
                        else:
                            if item["status"] == "pending":
                                item["file_intents"] = file_intents(db, obj, item, deadline, MAX_FILES - run.summary.get("files_deleted", 0))
                                item["status"] = "deleting"
                                values = deepcopy(run.items)
                                values[index] = item
                                run.items, run.heartbeat_at = values, at
                                # Durable intent precedes irreversible unlink. Re-entry can reconcile missing files.
                                db.commit()
                            outcome = process_item(db, run, item, at, deadline)
                        values = deepcopy(run.items)
                        values[index] = outcome
                        run.items, run.summary, run.heartbeat_at = values, run_totals(values), at
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        run = db.get(RetentionRun, uuid.UUID(str(run_id)))
                        values = deepcopy(run.items)
                        values[index] = {**item, "status": "failed", "reason": exc.code if isinstance(exc, FileSafetyError) else "cleanup_interrupted",
                                         "files_deleted": (outcome or {}).get("files_deleted", 0), "bytes_deleted": (outcome or {}).get("bytes_deleted", 0)}
                        run.items, run.summary = values, run_totals(values)
                        run.error_message = "部分清理未完成；文件操作不能随数据库回滚，原始意图与结果已保留，后续预览会重新核验。"
                        if isinstance(exc, FileSafetyError) and item["category"] in {"raw_documents", "reports"}:
                            model = RawDocument if item["category"] == "raw_documents" else Report
                            record = db.get(model, uuid.UUID(item["target_id"]))
                            if record:
                                record.retention = {**record.retention, "run_id": str(run.id), "last_error": exc.code,
                                                    "retry_after": stamp(at + timedelta(days=1))}
                        db.commit()
                run.summary = run_totals(run.items)
                pending = any(item["status"] in {"pending", "deleting"} for item in run.items)
                run.status = "partial" if pending or run.summary["failed"] else "succeeded"
                run.finished_at = now()
                if pending:
                    run.error_message = "已达到本轮时间预算；未处理项留待新的有界预览。"
                db.add(AuditEvent(user_id=run.actor_id, action="retention.run.finished", target=str(run.id),
                                 detail={"status": run.status, **run.summary}))
                db.commit()
                return run_response(run)
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(hashtextextended('retention-execution', 0))"))
            connection.commit()


def run_retention(trigger="scheduled", limit=100, run_id=None):
    if run_id is not None:
        return execute_run(run_id)
    with session_factory()() as db:
        waiting = db.scalar(select(RetentionRun.id).where(RetentionRun.status.in_(["queued", "running"]))
                            .order_by(RetentionRun.created_at).limit(1))
        if waiting:
            return execute_run(waiting)
        at = now()
        preview = create_preview(db, limit=min(limit, MAX_BATCH), at=at)
        if not preview.items:
            return {"status": "idle", "preview_id": str(preview.id), "eligible_count": 0,
                    "reason": "no_due_records_or_policy_grace"}
        key = "scheduled:" + str(preview.id)
        run, _ = create_run(db, preview.id, key, trigger=trigger, at=at)
        identifier = run.id
    return execute_run(identifier)
