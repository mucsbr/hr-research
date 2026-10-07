"""Attempt-fenced, age-limited official-site checkpoints with content-addressed evidence."""
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
import uuid

from sqlalchemy import func, select

from app.checkpoint_models import OfficialCheckpoint, OfficialCheckpointStep
from app.collection import CollectionError, CollectionResult, JobDocument
from app.config import get_settings
from app.db import session_factory
from app.models import SourceBinding, SourceRun, TaskEvent, TaskRun, now
from app.serialization import row

MAX_STATE_BYTES = 200_000
FILTER_KEYS = ("cities", "keywords", "exclusions", "sources", "max_pages", "max_details")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def binding_fingerprint(binding):
    # Operational health/updated_at may change after partial ingestion; only source
    # configuration determines reuse. The last authorized configuration is mandatory.
    return digest({key: binding.get(key) for key in (
        "id", "company_id", "kind", "entry_url", "adapter", "external_company_id", "account_id", "config", "enabled"
    )})


def scope_fingerprint(task, filters):
    settings = get_settings()
    return digest({"contract": "official-checkpoint-v1", "company_id": str(task.company_id),
                   "identity": task.config_snapshot.get("company_identity"),
                   "filters": {key: filters.get(key) for key in FILTER_KEYS},
                   "caps": [settings.task_max_pages, settings.task_max_details]})


def bounded_json(value):
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_STATE_BYTES:
        raise CollectionError("checkpoint_state_budget", "检查点游标超过预算，请缩小采集范围")


@dataclass
class CheckpointPage:
    key: str
    requested_url: str
    final_url: str
    body: str
    mode: str
    captured_at: datetime


class OfficialCheckpointStore:
    def __init__(self, identifier, task_id, binding, filters, expected_attempt, *, resumed=False, resume_from=None):
        self.id = identifier
        self.task_id = uuid.UUID(str(task_id))
        self.binding = binding
        self.binding_id = uuid.UUID(str(binding["id"]))
        self.filters = filters
        self.expected_attempt = expected_attempt
        self.resumed = resumed
        self.resume_from = str(resume_from) if resume_from else None
        self.pending = {}
        self.cached_keys, self.new_keys = set(), set()

    @classmethod
    def open(cls, task_id, binding, filters, source_run_id, expected_attempt, resume_from_task_id=None):
        task_id, binding_id = uuid.UUID(str(task_id)), uuid.UUID(str(binding["id"]))
        with session_factory()() as db:
            task = db.scalar(select(TaskRun).where(TaskRun.id == task_id).with_for_update()
                             .execution_options(populate_existing=True))
            cls._task_guard(task, expected_attempt)
            run = db.get(SourceRun, uuid.UUID(str(source_run_id)))
            if not run or run.task_id != task_id or run.binding_id != binding_id:
                raise CollectionError("checkpoint_target_mismatch", "检查点与任务来源不匹配")
            current = db.get(SourceBinding, binding_id)
            requested_fp = binding_fingerprint(binding)
            scope = scope_fingerprint(task, filters)
            checkpoint = db.scalar(select(OfficialCheckpoint).where(
                OfficialCheckpoint.task_id == task_id, OfficialCheckpoint.binding_id == binding_id).with_for_update())
            resumed, previous_id = checkpoint is not None, None
            if checkpoint is None:
                previous = None
                if resume_from_task_id:
                    previous_id = uuid.UUID(str(resume_from_task_id))
                    previous_task = db.get(TaskRun, previous_id)
                    if not previous_task or previous_task.company_id != task.company_id or previous_task.status in {"running", "queued"}:
                        raise CollectionError("checkpoint_target_mismatch", "只能接续同公司已结束任务的明确检查点")
                    previous = db.scalar(select(OfficialCheckpoint).where(
                        OfficialCheckpoint.task_id == previous_id, OfficialCheckpoint.binding_id == binding_id))
                    if previous:
                        old_run = db.get(SourceRun, previous.source_run_id)
                        if previous.status == "complete" and old_run.status in {"succeeded", "partial"}:
                            previous = None  # A finished observation is a fresh collection, not a resume.
                if previous:
                    cls._fingerprint_guard(previous, current, requested_fp, scope, fresh_task=True)
                    cls._age_guard(db, previous.id)
                    cls._budget_guard(db, previous)
                elif not current or binding_fingerprint(row(current)) != requested_fp:
                    raise CollectionError("source_changed", "来源配置已改变，请创建新的采集任务")
                checkpoint = OfficialCheckpoint(
                    task_id=task_id, binding_id=binding_id, source_run_id=run.id, attempt=expected_attempt,
                    scope_fingerprint=scope, binding_fingerprints=(list(previous.binding_fingerprints) if previous else [requested_fp]),
                    plan=dict(previous.plan) if previous else {}, plan_fingerprint=previous.plan_fingerprint if previous else "",
                    state=json.loads(json.dumps(previous.state)) if previous else {},
                    status=previous.status if previous else "collecting", raw_bytes=0,
                )
                if previous_id:
                    checkpoint.state = {**checkpoint.state, "resume_from_task_id": str(previous_id)}
                db.add(checkpoint)
                db.flush()
                if previous:
                    for step in db.scalars(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == previous.id)):
                        cls._read_body(step)
                        values = row(step, exclude={"id", "created_at", "updated_at", "checkpoint_id", "captured_at"})
                        db.add(OfficialCheckpointStep(checkpoint_id=checkpoint.id, captured_at=step.captured_at, **values))
                        checkpoint.raw_bytes += step.byte_size
                    resumed = True
            else:
                cls._fingerprint_guard(checkpoint, current, requested_fp, scope)
                cls._age_guard(db, checkpoint.id)
                cls._budget_guard(db, checkpoint)
                checkpoint.attempt = expected_attempt
                previous_id = checkpoint.state.get("resume_from_task_id")
            db.commit()
            return cls(checkpoint.id, task_id, binding, filters, expected_attempt, resumed=resumed, resume_from=previous_id)

    @staticmethod
    def _task_guard(task, attempt):
        if task is None or task.attempt != attempt or task.status != "running":
            raise CollectionError("execution_superseded", "旧任务代次不能修改或提交检查点")
        if task.cancel_requested:
            raise CollectionError("cancelled", "任务已取消，已完成检查点保持不变")
        if task.started_at and (now() - task.started_at).total_seconds() > get_settings().task_timeout_seconds:
            raise CollectionError("time_budget", "任务达到时间预算，已完成检查点已保留")

    @staticmethod
    def _fingerprint_guard(checkpoint, binding, requested, scope, fresh_task=False):
        allowed = checkpoint.binding_fingerprints
        if (not allowed or checkpoint.scope_fingerprint != scope or not binding
                or binding_fingerprint(row(binding)) != allowed[-1]
                or requested not in allowed or fresh_task and requested != allowed[-1]):
            raise CollectionError("checkpoint_scope_changed", "网址、范围或采集规则已改变，旧检查点不可复用；请新建采集")

    @staticmethod
    def _age_guard(db, identifier):
        first = db.scalar(select(func.min(OfficialCheckpointStep.captured_at)).where(
            OfficialCheckpointStep.checkpoint_id == identifier))
        limit = max(1, getattr(get_settings(), "checkpoint_max_age_seconds", 3600))
        if first and (now() - first).total_seconds() > limit:
            raise CollectionError("checkpoint_expired", "检查点超过集合时间窗口，请新建任务重新采集，不能作为完整新快照")

    @staticmethod
    def _budget_guard(db, checkpoint):
        settings = get_settings()
        if checkpoint.raw_bytes > getattr(settings, "checkpoint_max_raw_bytes", 30_000_000):
            raise CollectionError("checkpoint_raw_budget", "已有检查点超过当前原文预算，未复用")
        count, records = db.execute(select(func.count(), func.coalesce(func.sum(
            func.jsonb_array_length(OfficialCheckpointStep.parsed_records)), 0)).where(
                OfficialCheckpointStep.checkpoint_id == checkpoint.id)).one()
        if count > getattr(settings, "checkpoint_max_steps", 600):
            raise CollectionError("checkpoint_step_budget", "已有检查点超过当前步骤预算，未复用")
        if records > getattr(settings, "checkpoint_max_records", 500):
            raise CollectionError("checkpoint_record_budget", "已有检查点超过当前岗位预算，未复用")

    def _guard(self, db):
        task = db.scalar(select(TaskRun).where(TaskRun.id == self.task_id).with_for_update()
                         .execution_options(populate_existing=True))
        self._task_guard(task, self.expected_attempt)
        checkpoint = db.scalar(select(OfficialCheckpoint).where(OfficialCheckpoint.id == self.id).with_for_update()
                               .execution_options(populate_existing=True))
        if checkpoint.attempt != self.expected_attempt:
            raise CollectionError("execution_superseded", "检查点已由新代次接管")
        self._fingerprint_guard(checkpoint, db.get(SourceBinding, self.binding_id, populate_existing=True),
                                binding_fingerprint(self.binding), scope_fingerprint(task, self.filters))
        self._age_guard(db, self.id)
        self._budget_guard(db, checkpoint)
        task.heartbeat_at = now()
        return checkpoint

    def assert_current(self):
        with session_factory()() as db:
            self._guard(db)
            db.commit()

    def guard_in_session(self, db):
        """Use inside the worker's existing fenced ingestion transaction; never commit it."""
        return self._guard(db)

    @staticmethod
    def _read_body(step):
        root = (get_settings().storage_dir / "checkpoints").resolve()
        path = get_settings().storage_dir / step.storage_ref
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise CollectionError("checkpoint_evidence_missing", "检查点原始证据缺失，请新建任务采集")
        if path.stat().st_size != step.byte_size:
            raise CollectionError("checkpoint_evidence_changed", "检查点原始证据大小已变化，未复用缓存")
        data = path.read_bytes()
        if len(data) != step.byte_size or hashlib.sha256(data).hexdigest() != step.sha256:
            raise CollectionError("checkpoint_evidence_changed", "检查点原始证据校验失败，未复用缓存")
        return data.decode("utf-8")

    def fetch(self, fetcher, url, *, mode="http"):
        key = digest({"mode": mode, "url": url})
        self.assert_current()
        if key in self.pending:
            return self.pending[key]
        with session_factory()() as db:
            step = db.scalar(select(OfficialCheckpointStep).where(
                OfficialCheckpointStep.checkpoint_id == self.id, OfficialCheckpointStep.request_key == key))
            if step:
                page = CheckpointPage(key, url, step.final_url, self._read_body(step), mode, step.captured_at)
                self.cached_keys.add(key)
                self.pending[key] = page
                return page
        if hasattr(fetcher, "check"):
            fetcher.check = self.assert_current
        final, body = fetcher.fetch(url)
        if len(body.encode()) > 3_000_000:
            raise CollectionError("document_too_large", "单页检查点原文超过采集预算")
        page = CheckpointPage(key, url, final, body, mode, now())
        self.assert_current()  # Cancel/attempt replacement during HTTP cannot advance progress.
        self.pending[key] = page
        self.new_keys.add(key)
        return page

    def cursor(self, collector=None):
        with session_factory()() as db:
            checkpoint = self._guard(db)
            cursor = checkpoint.state.get("cursor", {})
            if collector and cursor and cursor.get("collector") != collector:
                raise CollectionError("checkpoint_strategy_changed", "检查点采集策略不匹配")
            value = json.loads(json.dumps(cursor))
            db.commit()
            return value

    def save(self, cursor, *, page=None, records=None, metadata=None, status="collecting"):
        bounded_json(cursor)
        if metadata is not None:
            bounded_json(metadata)
        settings = get_settings()
        with session_factory()() as db:
            checkpoint = self._guard(db)
            if page is not None:
                step = db.scalar(select(OfficialCheckpointStep).where(
                    OfficialCheckpointStep.checkpoint_id == self.id, OfficialCheckpointStep.request_key == page.key))
                data = page.body.encode()
                body_hash = hashlib.sha256(data).hexdigest()
                if step is None:
                    count = db.scalar(select(func.count()).select_from(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == self.id))
                    if count >= getattr(settings, "checkpoint_max_steps", 600):
                        raise CollectionError("checkpoint_step_budget", "检查点页数已达总上限，请缩小范围")
                    if checkpoint.raw_bytes + len(data) > getattr(settings, "checkpoint_max_raw_bytes", 30_000_000):
                        raise CollectionError("checkpoint_raw_budget", "检查点原文总量已达上限，未标记完整覆盖")
                    folder = settings.storage_dir / "checkpoints" / str(self.id)
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target = folder / (body_hash + ".txt")
                    if not target.exists():
                        temporary = target.with_suffix("." + uuid.uuid4().hex + ".tmp")
                        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(data)
                        os.replace(temporary, target)
                    step = OfficialCheckpointStep(
                        checkpoint_id=self.id, request_key=page.key, requested_url=page.requested_url,
                        final_url=page.final_url, content_type="application/json" if page.body.lstrip().startswith(("{", "[")) else "text/html",
                        sha256=body_hash, storage_ref=str(target.relative_to(settings.storage_dir)), byte_size=len(data),
                        kind=page.mode, captured_at=page.captured_at, parsed_records=[], step_metadata={},
                    )
                    db.add(step)
                    checkpoint.raw_bytes += len(data)
                elif step.sha256 != body_hash:
                    raise CollectionError("checkpoint_evidence_changed", "同一步骤证据发生变化，未混用旧结果")
                if records is not None:
                    for document in records:
                        document.observed_at = page.captured_at
                    values = [document.model_dump(mode="json") for document in records]
                    old_count = sum(len(value or []) for value in db.scalars(select(OfficialCheckpointStep.parsed_records).where(
                        OfficialCheckpointStep.checkpoint_id == self.id, OfficialCheckpointStep.request_key != page.key)))
                    if old_count + len(values) > getattr(settings, "checkpoint_max_records", 500):
                        raise CollectionError("checkpoint_record_budget", "检查点岗位总量已达上限，请缩小范围")
                    step.parsed_records = values
                if metadata is not None:
                    step.step_metadata = metadata
            checkpoint.state = {**checkpoint.state, "cursor": cursor}
            checkpoint.status = status
            db.commit()

    def plan(self):
        with session_factory()() as db:
            checkpoint = self._guard(db)
            value = dict(checkpoint.plan)
            db.commit()
            return value

    def reserve_adaptation(self):
        with session_factory()() as db:
            checkpoint = self._guard(db)
            if checkpoint.state.get("adaptation_started"):
                raise CollectionError("checkpoint_adaptation_unconfirmed", "本任务已申请过规则适配，未自动重复调用模型；请核对规则后新建任务")
            checkpoint.state = {**checkpoint.state, "adaptation_started": True}
            db.commit()

    def release_unsent_adaptation(self):
        with session_factory()() as db:
            checkpoint = self._guard(db)
            checkpoint.state = {**checkpoint.state, "adaptation_started": False}
            db.commit()

    def note(self, message, detail):
        with session_factory()() as db:
            self._guard(db)
            db.add(TaskEvent(task_id=self.task_id, message=message, detail=detail))
            db.commit()

    def remember_plan(self, plan, call_id, model):
        """Atomically authorize only this validated automatic binding-plan transition."""
        bounded_json(plan)
        with session_factory()() as db:
            checkpoint = self._guard(db)
            binding = db.scalar(select(SourceBinding).where(SourceBinding.id == self.binding_id).with_for_update())
            plan_hash = plan_digest(plan)
            checkpoint.plan, checkpoint.plan_fingerprint = plan, plan_hash
            checkpoint.state = {**checkpoint.state, "cursor": {}}
            for step in db.scalars(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == self.id)):
                step.parsed_records = []
            binding.config = {**binding.config, "website_plan": plan, "plan_hash": plan_hash,
                              "plan_call_id": str(call_id), "plan_model": model}
            db.flush()
            checkpoint.binding_fingerprints = list(dict.fromkeys([*checkpoint.binding_fingerprints, binding_fingerprint(row(binding))]))
            db.commit()

    def result(self, cursor, coverage, stop_reason, *, finished=False):
        bounded_json(cursor)
        with session_factory()() as db:
            checkpoint = self._guard(db)
            steps = db.scalars(select(OfficialCheckpointStep).where(OfficialCheckpointStep.checkpoint_id == self.id)
                               .order_by(OfficialCheckpointStep.captured_at, OfficialCheckpointStep.request_key)).all()
            records, seen = [], set()
            for step in steps:
                for value in step.parsed_records:
                    document = JobDocument.model_validate({**value, "observed_at": step.captured_at})
                    if document.source_job_id not in seen:
                        records.append(document)
                        seen.add(document.source_job_id)
            documents = [(step.final_url, self._read_body(step), step.content_type) for step in steps]
            times = [step.captured_at for step in steps]
            # A missing/tampered artifact must not commit a 'complete' checkpoint
            # that the worker would skip on its next source-level retry.
            checkpoint.state = {**checkpoint.state, "cursor": cursor}
            checkpoint.status = "complete" if finished else "partial"
            result = CollectionResult(records=records, documents=documents, coverage=coverage,
                pages=cursor.get("pages", 0), stop_reason=stop_reason, errors=list(cursor.get("errors", [])),
                adapter_fingerprint=checkpoint.plan_fingerprint,
                checkpoint_info={"checkpoint_id": str(self.id), "resumed": self.resumed,
                    "resume_from_task_id": self.resume_from, "status": checkpoint.status,
                    "cached_steps": len(self.cached_keys), "new_steps": len(self.new_keys),
                    "collection_window_start": min(times).isoformat() if times else None,
                    "collection_window_end": max(times).isoformat() if times else None})
            db.commit()
            return result

    def records(self):
        with session_factory()() as db:
            checkpoint = self._guard(db)
            result = [JobDocument.model_validate(value) for values in db.scalars(
                select(OfficialCheckpointStep.parsed_records).where(OfficialCheckpointStep.checkpoint_id == checkpoint.id))
                for value in values]
            db.commit()
            return result

    def records_for(self, page):
        with session_factory()() as db:
            self._guard(db)
            step = db.scalar(select(OfficialCheckpointStep).where(
                OfficialCheckpointStep.checkpoint_id == self.id, OfficialCheckpointStep.request_key == page.key))
            values = [JobDocument.model_validate({**value, "observed_at": step.captured_at}) for value in step.parsed_records] if step else []
            db.commit()
            return values
