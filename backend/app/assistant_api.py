"""A disclosed, bounded Chinese command grammar using existing platform services.

Unmatched instructions never cause implicit model calls. Writes are signed drafts
and explicit execution; reads are direct queries. Execution receipts are durable.
"""
import hashlib
import json
import re
import uuid
from datetime import timedelta

from fastapi import APIRouter, HTTPException
import jwt
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.analytics_service import company_metrics, create_comparison
from app.collection import JobDocument, matches_filters
from app.config import get_settings
from app.models import AuditEvent, Company, Job, JobSourceRecord, MonitoringPlan, SourceBinding, Subject, now
from app.recruitment_discovery import bulk_companies, normalize_name
from app.schemas import Input, PlanInput, RunInput, ScopeFilters
from app.security import CurrentUser, Db
from app.serialization import row
from app.tasks import create_analysis, create_batch

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

EXAMPLES = [
    "添加公司：星河科技、远山科技",
    "更新招聘：星河科技；城市=上海；关键词=研发；来源=官网,BOSS",
    "查询岗位：星河科技；城市=上海；关键词=工程师",
    "比较招聘：星河科技、远山科技；天数=30",
    "每天监测：星河科技；时间=09:00；城市=上海；关键词=研发",
    "暂停计划：星河科技招聘监测",
    "生成分析：星河科技",
]
SOURCES = {"官网": "careers", "boss": "boss", "boss直聘": "boss", "猎聘": "liepin",
           "智联": "zhaopin", "智联招聘": "zhaopin", "前程无忧": "51job"}


class MessageInput(Input):
    message: str = Field(min_length=1, max_length=3000)


class ExecuteInput(Input):
    action_token: str = Field(min_length=1, max_length=16000)


def split_names(value):
    values = list(dict.fromkeys(part.strip() for part in re.split(r"[、,，\n]", value) if part.strip()))
    if not values or len(values) > 20 or any(len(part) > 250 for part in values):
        raise ValueError("一次操作需要1至20个明确公司名，可用顿号或逗号分隔")
    return values


def parse_message(message):
    parts = re.split(r"[；;]", message.strip())
    lead, options = parts[0].strip(), {}
    for part in parts[1:]:
        if not part.strip():
            continue
        key, separator, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in {"城市", "关键词", "排除词", "来源", "天数", "时间"} or not value:
            raise ValueError("附加条件请使用示例中的 城市=、关键词=、来源=、天数=、时间= 格式")
        options[key] = value
    grammar = {
        "add_companies": r"^(?:添加公司|关注公司)\s*[:：]\s*(.+)$",
        "collect": r"^更新招聘\s*[:：]\s*(.+)$",
        "query": r"^查询岗位\s*[:：]\s*(.+)$",
        "compare": r"^比较招聘\s*[:：]\s*(.+)$",
        "create_plan": r"^每天监测\s*[:：]\s*(.+)$",
        "pause_plan": r"^暂停计划\s*[:：]\s*(.+)$",
        "analyze": r"^生成分析\s*[:：]\s*(.+)$",
    }
    intent, target = None, None
    for key, pattern in grammar.items():
        match = re.match(pattern, lead)
        if match:
            intent, target = key, match[1].strip()
            break
    if not intent:
        patterns = [
            ("collect", r"^现在更新\s*(.+?)\s*的招聘$"),
            ("query", r"^(.+?)\s*最近一个月主要招什么人[？?]?$"),
            ("compare", r"^比较\s*(.+?)\s*最近一个月的招聘变化[？?]?$"),
            ("pause_company", r"^暂停\s*(.+?)\s*的监测$"),
            ("add_companies", r"^关注\s*(.+?)\s*(?:两家公司|这些公司)$"),
        ]
        for key, pattern in patterns:
            match = re.match(pattern, lead)
            if match:
                intent, target = key, match[1].strip()
                break
    if not intent:
        match = re.match(r"^以后每天关注\s*(.+?)\s*在\s*(.+?)\s*的\s*(.+?)岗位$", lead)
        if match:
            intent, target = "create_plan", match[1].strip()
            options.setdefault("城市", match[2].strip())
            options.setdefault("关键词", match[3].strip())
    if not intent:
        return None
    permitted_options = {
        "collect": {"城市", "关键词", "排除词", "来源"},
        "query": {"城市", "关键词", "排除词", "来源", "天数"},
        "compare": {"天数"}, "create_plan": {"城市", "关键词", "排除词", "来源", "时间"},
    }.get(intent, set())
    if set(options) - permitted_options:
        raise ValueError("此类操作暂不支持所附条件，未忽略条件执行")
    days = int(options.get("天数", "30"))
    if not 1 <= days <= 365:
        raise ValueError("天数应为1至365")
    filters = {"cities": split_names(options["城市"]) if options.get("城市") else [],
               "keywords": split_names(options["关键词"]) if options.get("关键词") else [],
               "exclusions": split_names(options["排除词"]) if options.get("排除词") else [], "sources": []}
    if options.get("来源"):
        for source in split_names(options["来源"]):
            mapped = SOURCES.get(source.casefold())
            if not mapped:
                raise ValueError("来源仅支持官网、BOSS、猎聘、智联、前程无忧")
            filters["sources"].append(mapped)
    time = options.get("时间", "09:00")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time):
        raise ValueError("时间请填写24小时制，例如09:00")
    return {"intent": intent, "target": target, "names": split_names(target) if intent != "pause_plan" else [],
            "filters": ScopeFilters.model_validate(filters).model_dump(), "days": days, "time": time}


def resolve_companies(db, names):
    companies = db.scalars(select(Company).where(~Company.archived)).all()
    resolved, issues = [], []
    for name in names:
        normalized = normalize_name(name)
        matches = []
        for company in companies:
            subject = db.get(Subject, company.subject_id)
            values = [company.name, company.official_name or "", *(subject.aliases if subject else [])]
            if str(company.id) == name or normalized in {normalize_name(value) for value in values if value}:
                matches.append(company)
        if len(matches) == 1:
            if matches[0].id not in {company.id for company in resolved}:
                resolved.append(matches[0])
        else:
            issues.append({"input": name, "status": "ambiguous" if matches else "not_found",
                           "candidates": [{"id": str(company.id), "name": company.name,
                                           "official_name": company.official_name, "website": company.website} for company in matches],
                           "instruction": "请在指令中使用目标公司ID" if matches else "请先添加公司或使用已登记名称"})
    return resolved, issues


def draft_action(user_id, action, payload, summary):
    secret = get_settings().app_secret.get_secret_value()
    if not secret:
        raise HTTPException(503, "对话动作签名未配置")
    identifier = str(uuid.uuid4())
    expires = now() + timedelta(minutes=15)
    token = jwt.encode({"jti": identifier, "sub": str(user_id), "aud": "hr-assistant-action",
                        "exp": expires, "iat": now(), "action": action, "payload": payload}, secret, algorithm="HS256")
    conditional_model = action in {"collect", "create_plan"}
    if conditional_model:
        summary += " 新官网或页面改版时，采集执行可能申请受预算限制的模型适配；本次指令解析没有调用模型。"
    return {"type": "action_draft", "message": summary, "action": action, "payload": payload,
            "action_id": identifier, "action_token": token, "expires_at": expires.isoformat(),
            "requires_execution": True, "model_calls": 0,
            "model_usage": "conditional_website_adaptation" if conditional_model else "one_bounded_analysis" if action == "analyze" else "none",
            "estimated_model_calls": None if conditional_model else 1 if action == "analyze" else 0,
            "execution_endpoint": "/api/v1/assistant/actions/execute"}


def query_jobs(db, companies, filters, days):
    query = select(Job).where(Job.company_id.in_([company.id for company in companies]), Job.status != "merged")
    jobs = db.scalars(query.order_by(Job.updated_at.desc()).limit(1000)).all()
    results = []
    for job in jobs:
        record_query = select(JobSourceRecord).join(SourceBinding, JobSourceRecord.binding_id == SourceBinding.id).where(JobSourceRecord.job_id == job.id)
        if filters["sources"]:
            record_query = record_query.where(SourceBinding.kind.in_(filters["sources"]))
        records = db.scalars(record_query).all()
        records = [record for record in records if matches_filters(JobDocument(
            source_job_id=record.source_job_id, title=job.title, url=record.url, locations=job.locations,
            description=record.data.get("description") or "", requirements=record.data.get("requirements") or ""), filters)]
        if not records:
            continue
        results.append({**row(job), "source_urls": [record.url for record in records],
                        "last_observed_at": max((record.last_seen_at for record in records), default=None).isoformat() if records else None})
    metrics = [company_metrics(db, company.id, days=days) for company in companies]
    return {"type": "query_result", "message": "以下是已有数据库中的岗位与来源覆盖；未触发采集或模型。",
            "items": results[:100], "matched_in_scan": len(results), "scan_limit": 1000,
            "scan_truncated": len(jobs) == 1000, "total_matches": None if len(jobs) == 1000 else len(results),
            "returned_limit": 100, "companies": metrics, "model_calls": 0,
            "limitations": ["显示已有观察记录；疑似下架等状态保留，未宣称这些岗位现在一定仍在招聘。",
                            "最多扫描最近更新的1000个岗位，返回最多100个；达到扫描上限时匹配总数未知。"]}


@router.get("/capabilities")
def capabilities(user: CurrentUser):
    return {"mode": "bounded_chinese_commands", "examples": EXAMPLES, "unmatched_model_calls": 0,
            "writes": "signed_draft_then_explicit_execution", "timezone": "Asia/Shanghai",
            "limitations": ["当前支持已列出的中文指令格式，不是通用自然语言代理。"]}


@router.post("/messages")
def message(data: MessageInput, db: Db, user: CurrentUser):
    try:
        parsed = parse_message(data.message)
    except (ValueError, TypeError):
        return {"type": "clarification", "message": "指令参数不完整或格式无效，请参照示例。", "examples": EXAMPLES, "model_calls": 0}
    if not parsed:
        return {"type": "unsupported", "message": "当前尚未匹配此表达，没有执行操作或调用模型。", "examples": EXAMPLES, "model_calls": 0}
    intent = parsed["intent"]
    if intent == "add_companies":
        return draft_action(user.id, intent, {"names": parsed["names"]}, "添加以下关注公司：" + "、".join(parsed["names"]) + "。同名对象不会自动合并；招聘入口后续通过公司页配置。")
    if intent == "pause_plan":
        candidates = db.scalars(select(MonitoringPlan).where(MonitoringPlan.domain == "recruitment")).all()
        candidates = [plan for plan in candidates if str(plan.id) == parsed["target"] or normalize_name(plan.name) == normalize_name(parsed["target"])]
        if len(candidates) != 1:
            return {"type": "clarification", "message": "未唯一确定要暂停的计划，请使用计划ID。",
                    "plans": [row(plan) for plan in candidates], "model_calls": 0}
        plan = candidates[0]
        return draft_action(user.id, "pause_plan", {"plan_id": str(plan.id), "company_ids": plan.company_ids},
                            f"暂停计划“{plan.name}”的后续运行，涉及{len(plan.company_ids)}家公司；历史与当前任务保留。")
    companies, issues = resolve_companies(db, parsed["names"])
    if issues:
        return {"type": "clarification", "message": "公司身份需要明确，尚未执行任何操作。", "issues": issues, "model_calls": 0}
    identifiers = [str(company.id) for company in companies]
    names = "、".join(company.name for company in companies)
    if intent == "query":
        return query_jobs(db, companies, parsed["filters"], parsed["days"])
    if intent == "pause_company":
        plans = db.scalars(select(MonitoringPlan).where(MonitoringPlan.domain == "recruitment", MonitoringPlan.enabled.is_(True))).all()
        relevant = [plan for plan in plans if set(plan.company_ids) & set(identifiers)]
        if len(relevant) != 1 or set(relevant[0].company_ids) - set(identifiers):
            return {"type": "clarification", "message": "需要明确暂停哪个计划；共用计划可能同时影响其他公司。请用“暂停计划：计划ID”。",
                    "plans": [row(plan) for plan in relevant], "model_calls": 0}
        return draft_action(user.id, "pause_plan", {"plan_id": str(relevant[0].id), "company_ids": identifiers},
                            "暂停“" + relevant[0].name + "”的后续运行，保留历史数据。")
    if intent == "compare":
        if len(companies) < 2:
            return {"type": "clarification", "message": "比较需要至少两家不同公司。", "model_calls": 0}
        return draft_action(user.id, intent, {"company_ids": identifiers, "days": parsed["days"]},
                            f"比较{names}最近{parsed['days']}天的已有招聘数据；不触发采集，不调用模型。")
    if intent == "analyze":
        if len(companies) != 1:
            return {"type": "clarification", "message": "分析任务一次针对一家公司；跨公司请使用比较招聘。", "model_calls": 0}
        return draft_action(user.id, intent, {"company_id": identifiers[0]},
                            names + "：使用已有岗位创建独立分析，执行时最多一次受预算限制的模型请求。")
    if intent == "create_plan":
        hour, minute = map(int, parsed["time"].split(":"))
        payload = {"name": names[:150] + "招聘监测", "company_ids": identifiers, "filters": parsed["filters"],
                   "schedule": f"{minute} {hour} * * *", "enabled": True, "timezone": "Asia/Shanghai"}
        return draft_action(user.id, intent, payload, f"每天北京时间{parsed['time']}采集{names}；每家公司独立任务，筛选范围见动作内容。")
    return draft_action(user.id, "collect", {"company_ids": identifiers, "filters": parsed["filters"], "name": "对话触发招聘采集"},
                        "立即采集" + names + "，每家公司创建独立任务；缺少来源或登录时显示待处理原因。")


@router.post("/actions/execute")
def execute_action(data: ExecuteInput, db: Db, user: CurrentUser):
    try:
        signed = jwt.decode(data.action_token, get_settings().app_secret.get_secret_value(),
                            algorithms=["HS256"], audience="hr-assistant-action",
                            options={"require": ["exp", "iat", "sub", "jti", "action", "payload"]})
        action_id = uuid.UUID(signed["jti"])
        if signed["sub"] != str(user.id):
            raise ValueError("actor mismatch")
    except (jwt.PyJWTError, ValueError, KeyError):
        raise HTTPException(422, "动作签名已失效或不属于当前用户，请重新生成动作") from None
    payload, action = signed["payload"], signed["action"]
    digest = hashlib.sha256(json.dumps({"action": action, "payload": payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    # Atomic durable receipt prevents double execution even though legacy services commit internally.
    claimed = db.execute(insert(AuditEvent).values(id=action_id, user_id=user.id, action="assistant.execution",
                         target=str(action_id), detail={"state": "executing", "payload_hash": digest, "kind": action})
                         .on_conflict_do_nothing(index_elements=["id"]).returning(AuditEvent.id)).scalar()
    db.commit()
    receipt = db.get(AuditEvent, action_id)
    if not claimed:
        if receipt.user_id != user.id or receipt.detail.get("payload_hash") != digest:
            raise HTTPException(409, "动作记录与签名不一致")
        state = receipt.detail.get("state")
        if state == "completed":
            return {"state": "completed", "replayed": True, **receipt.detail["result"]}
        raise HTTPException(409, "动作正在执行或结果需要核对，未重复执行；请查看相关任务或计划")
    try:
        if action == "add_companies":
            result = bulk_companies(db, [{"name": name} for name in payload["names"]], user.id)
            db.commit()
        elif action == "collect":
            batch, created = create_batch(db, RunInput(**payload, idempotency_key="assistant:" + str(action_id)), trigger="conversation")
            result = {"batch_id": str(batch.id), "created": created}
        elif action == "compare":
            task, created = create_comparison(db, [uuid.UUID(identifier) for identifier in payload["company_ids"]],
                                              days=payload["days"], idempotency_key="assistant:" + str(action_id))
            result = {"task_id": str(task.id), "created": created, "model_calls": 0}
        elif action == "analyze":
            task, created = create_analysis(db, uuid.UUID(payload["company_id"]))
            result = {"task_id": str(task.id), "created": created}
        elif action == "create_plan":
            from app.api import add_plan
            result = add_plan(PlanInput.model_validate(payload), db, user)
        elif action == "pause_plan":
            from app.api import pause_plan
            result = pause_plan(uuid.UUID(payload["plan_id"]), db, user)
        else:
            raise HTTPException(422, "动作类型不受支持")
        receipt = db.get(AuditEvent, action_id)
        receipt.detail = {**receipt.detail, "state": "completed", "result": result}
        db.commit()
        return {"state": "completed", "replayed": False, **result}
    except HTTPException as exc:
        db.rollback()
        receipt = db.get(AuditEvent, action_id)
        receipt.detail = {**receipt.detail, "state": "failed", "reason": str(exc.detail)[:400]}
        db.commit()
        raise
    except Exception:
        db.rollback()
        # Receipt remains executing: outcome may have committed in an existing service.
        raise HTTPException(409, "执行结果需要核对，未自动重复操作；请查看任务或计划记录") from None
