import uuid
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, SecretStr, field_validator, model_validator
from sqlalchemy import select

from app.models import AuditEvent, Subject, now
from app.notification_models import (
    NewsPolicyReminder, NotificationChannel, NotificationDelivery, NotificationEvent, NotificationRule,
)
from app.notifications import (
    DeliveryError, EVENT_TYPES, NEWS_EVENTS, encrypt_config, lock_notification_state,
    news_delivery_error, normalized_tags, reconcile_news_deliveries, validate_recipient,
)
from app.notification_digest import cancel_pending, pending_groups, utc_stamp
from app.schemas import Input
from app.security import AdminUser, CurrentUser, Db
from app.serialization import row

router = APIRouter(prefix="/api/v1")


def require(db, model, identifier):
    result = db.get(model, identifier)
    if result is None:
        raise HTTPException(404, "记录不存在")
    return result


class ChannelInput(Input):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["email", "webhook", "feishu"]
    url: SecretStr | None = Field(default=None, max_length=2000)
    recipient: str | None = Field(default=None, max_length=250)
    secret: SecretStr = SecretStr("")
    enabled: bool = False

    @model_validator(mode="after")
    def destination(self):
        if self.kind == "email":
            try:
                validate_recipient(self.recipient)
            except DeliveryError:
                raise ValueError("邮件通道需要一个有效邮箱；多个收件人请分别配置通道") from None
        else:
            try:
                parsed = urlparse(self.url.get_secret_value() if self.url else "")
                port = parsed.port
            except (ValueError, TypeError):
                # urllib's parse errors can interpolate the raw netloc/port.
                raise ValueError("Webhook 地址或端口格式无效") from None
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Webhook 需要不含用户凭据与片段的 HTTP/HTTPS 地址")
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("Webhook 端口无效")
            if self.kind == "feishu" and (parsed.scheme != "https" or parsed.hostname != "open.feishu.cn"
                                         or port not in {None, 443}
                                         or not parsed.path.startswith("/open-apis/bot/v2/hook/")
                                         or not parsed.path.removeprefix("/open-apis/bot/v2/hook/")):
                raise ValueError("请填写飞书自定义机器人的 HTTPS Webhook 地址")
        return self


class EnabledInput(Input):
    enabled: bool


class RetryInput(Input):
    acknowledge_duplicate: bool = False


class NewsFilters(Input):
    regions: list[str] = Field(default_factory=list, max_length=30)
    industries: list[str] = Field(default_factory=list, max_length=30)
    topics: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("regions", "industries", "topics")
    @classmethod
    def valid_tags(cls, values):
        if any(not value.strip() or len(value) > 150 for value in values):
            raise ValueError("资讯标签不能为空白，单项最多150字")
        return normalized_tags(values)


class RuleInput(Input):
    name: str = Field(min_length=1, max_length=120)
    channel_id: uuid.UUID
    event_types: list[str] = Field(min_length=1, max_length=20)
    subject_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    news_filters: NewsFilters = Field(default_factory=NewsFilters)
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    delivery_mode: Literal["immediate", "daily_digest"] = "immediate"
    digest_time: str = Field(default="09:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    enabled: bool = False

    @model_validator(mode="after")
    def valid_filters(self):
        if set(self.event_types) - EVENT_TYPES:
            raise ValueError("不支持的通知事件")
        if any(self.news_filters.model_dump().values()) and set(self.event_types) - NEWS_EVENTS:
            raise ValueError("资讯标签条件只能用于资讯事件；其他事件请单独建规则")
        if "news.policy_effective" in self.event_types and self.delivery_mode != "immediate":
            raise ValueError("政策生效日提醒仅支持即时模式，固定在北京时间09:00处理")
        self.keywords = list(dict.fromkeys(word.strip() for word in self.keywords if word.strip()))
        if any(len(word) > 100 for word in self.keywords):
            raise ValueError("关键词过长")
        if self.delivery_mode == "daily_digest":
            self.cooldown_seconds = 0
        return self


def channel_result(channel):
    return row(channel, exclude={"encrypted_config"})


@router.get("/notification-event-types")
def event_types(user: CurrentUser):
    return sorted(EVENT_TYPES)


@router.get("/notification-channels")
def channels(db: Db, user: CurrentUser):
    return [channel_result(c) for c in db.scalars(select(NotificationChannel).order_by(
        NotificationChannel.created_at.desc()))]


def apply_channel(channel, data):
    config = {"recipient": data.recipient} if data.kind == "email" else {
        "url": data.url.get_secret_value(), "secret": data.secret.get_secret_value()}
    hint = data.recipient if data.kind == "email" else urlparse(config["url"]).hostname
    channel.name, channel.kind, channel.enabled = data.name, data.kind, data.enabled
    channel.destination_hint, channel.encrypted_config = hint, encrypt_config(config)


@router.post("/notification-channels", status_code=201)
def add_channel(data: ChannelInput, db: Db, user: AdminUser):
    lock_notification_state(db)
    channel = NotificationChannel()
    apply_channel(channel, data)
    db.add(channel)
    db.flush()
    db.add(AuditEvent(user_id=user.id, action="notification.channel_created", target=str(channel.id)))
    db.commit()
    return channel_result(channel)


@router.put("/notification-channels/{channel_id}")
def replace_channel(channel_id: uuid.UUID, data: ChannelInput, db: Db, user: AdminUser):
    lock_notification_state(db)
    channel = db.scalar(select(NotificationChannel).where(NotificationChannel.id == channel_id).with_for_update())
    if channel is None:
        raise HTTPException(404, "记录不存在")
    cancelled = cancel_pending(db, channel_id=channel.id, reason="channel_changed")
    apply_channel(channel, data)
    db.add(AuditEvent(user_id=user.id, action="notification.channel_updated", target=str(channel.id),
                      detail={"cancelled_pending_digest_items": cancelled}))
    db.commit()
    return channel_result(channel)


@router.patch("/notification-channels/{channel_id}")
def enable_channel(channel_id: uuid.UUID, data: EnabledInput, db: Db, user: AdminUser):
    lock_notification_state(db)
    channel = require(db, NotificationChannel, channel_id)
    channel.enabled = data.enabled
    if not data.enabled:
        cancel_pending(db, channel_id=channel.id, reason="channel_disabled")
    db.add(AuditEvent(user_id=user.id, action="notification.channel_enabled", target=str(channel.id),
                      detail={"enabled": data.enabled}))
    db.commit()
    return channel_result(channel)


@router.post("/notification-channels/{channel_id}/test", status_code=202)
def test_channel(channel_id: uuid.UUID, db: Db, user: AdminUser):
    lock_notification_state(db)
    channel = require(db, NotificationChannel, channel_id)
    if not channel.enabled:
        raise HTTPException(409, "请先启用该通道，再测试发送")
    event = NotificationEvent(type="notification.test", dedupe_key="test:" + str(uuid.uuid4()),
                              payload={"title": "观势通知测试", "text": "这是管理员发起的连接测试。", "url": ""})
    db.add(event)
    db.flush()
    delivery = NotificationDelivery(event_id=event.id, channel_id=channel.id)
    db.add(delivery)
    db.add(AuditEvent(user_id=user.id, action="notification.test_requested", target=str(channel.id)))
    db.commit()
    return {"delivery_id": str(delivery.id), "status": delivery.status}


@router.get("/notification-rules")
def rules(db: Db, user: CurrentUser):
    return [row(r) for r in db.scalars(select(NotificationRule).order_by(NotificationRule.created_at.desc()))]


def rule_values(data, db):
    require(db, NotificationChannel, data.channel_id)
    for subject in data.subject_ids:
        require(db, Subject, subject)
    values = data.model_dump()
    values["subject_ids"] = [str(value) for value in data.subject_ids]
    return values


@router.post("/notification-rules", status_code=201)
def add_rule(data: RuleInput, db: Db, user: AdminUser):
    lock_notification_state(db)
    rule = NotificationRule(**rule_values(data, db))
    db.add(rule)
    db.flush()
    db.add(AuditEvent(user_id=user.id, action="notification.rule_created", target=str(rule.id)))
    db.commit()
    return row(rule)


@router.put("/notification-rules/{rule_id}")
def replace_rule(rule_id: uuid.UUID, data: RuleInput, db: Db, user: AdminUser):
    lock_notification_state(db)
    rule = db.scalar(select(NotificationRule).where(NotificationRule.id == rule_id).with_for_update())
    if rule is None:
        raise HTTPException(404, "记录不存在")
    values = rule_values(data, db)
    previous_channel = rule.channel_id
    changed = any(getattr(rule, key) != value for key, value in values.items() if key != "name")
    cancelled = cancel_pending(db, rule_id=rule.id) if changed else 0
    for key, value in values.items():
        setattr(rule, key, value)
    cancelled_news = reconcile_news_deliveries(db, [previous_channel, rule.channel_id]) if changed else 0
    db.add(AuditEvent(user_id=user.id, action="notification.rule_updated", target=str(rule.id),
                      detail={"cancelled_pending_digest_items": cancelled,
                              "cancelled_pending_news_deliveries": cancelled_news}))
    db.commit()
    return {**row(rule), "cancelled_pending_digest_items": cancelled,
            "cancelled_pending_news_deliveries": cancelled_news}


@router.patch("/notification-rules/{rule_id}")
def enable_rule(rule_id: uuid.UUID, data: EnabledInput, db: Db, user: AdminUser):
    lock_notification_state(db)
    rule = db.scalar(select(NotificationRule).where(NotificationRule.id == rule_id).with_for_update())
    if rule is None:
        raise HTTPException(404, "记录不存在")
    rule.enabled = data.enabled
    cancelled_digest, cancelled_news = 0, 0
    if not data.enabled:
        cancelled_digest = cancel_pending(db, rule_id=rule.id, reason="digest_rule_disabled")
        cancelled_news = reconcile_news_deliveries(db, [rule.channel_id])
    db.add(AuditEvent(user_id=user.id, action="notification.rule_enabled", target=str(rule.id),
        detail={"enabled": data.enabled, "cancelled_pending_digest_items": cancelled_digest,
                "cancelled_pending_news_deliveries": cancelled_news}))
    db.commit()
    return row(rule)


@router.get("/notification-deliveries")
def deliveries(db: Db, user: CurrentUser, limit: int = Query(100, ge=1, le=200)):
    return [{**row(delivery), "event_type": event.type, "title": event.payload.get("title"),
             "channel_name": channel.name,
             "digest": {key: value for key, value in event.payload.get("digest", {}).items() if key not in {"items", "channel_updated_at"}} or None}
            for delivery, event, channel in db.execute(select(
                NotificationDelivery, NotificationEvent, NotificationChannel,
            ).join(NotificationEvent, NotificationDelivery.event_id == NotificationEvent.id)
                .join(NotificationChannel, NotificationDelivery.channel_id == NotificationChannel.id)
                .order_by(NotificationDelivery.created_at.desc()).limit(limit))]


@router.get("/notification-digest-pending")
def pending_digests(db: Db, user: CurrentUser):
    return pending_groups(db)


@router.get("/news-policy-reminders")
def policy_reminders(db: Db, user: CurrentUser, limit: int = Query(100, ge=1, le=200)):
    from app.news_models import NewsItem
    from app.repair_evidence import redact_text
    return [{**row(reminder, exclude={"context"}), "title": redact_text(item.title), "url": item.url}
            for reminder, item in db.execute(select(NewsPolicyReminder, NewsItem)
                .join(NewsItem, NewsPolicyReminder.item_id == NewsItem.id)
                .order_by(NewsPolicyReminder.scheduled_for.desc()).limit(limit))]


@router.get("/notification-deliveries/{delivery_id}")
def delivery_detail(delivery_id: uuid.UUID, db: Db, user: CurrentUser):
    delivery = require(db, NotificationDelivery, delivery_id)
    event = require(db, NotificationEvent, delivery.event_id)
    channel = require(db, NotificationChannel, delivery.channel_id)
    return {**row(delivery), "event_type": event.type, "channel_name": channel.name,
            "title": event.payload.get("title"), "text": event.payload.get("text"), "url": event.payload.get("url"),
            "digest": {key: value for key, value in event.payload.get("digest", {}).items() if key != "channel_updated_at"} or None}


@router.post("/notification-deliveries/{delivery_id}/retry", status_code=202)
def retry_delivery(delivery_id: uuid.UUID, db: Db, user: AdminUser, data: RetryInput | None = None):
    lock_notification_state(db)
    delivery = db.scalar(select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
                         .with_for_update())
    if not delivery:
        raise HTTPException(404, "记录不存在")
    if delivery.status not in {"failed", "unknown", "cancelled"}:
        raise HTTPException(409, "该记录不能重新发送")
    event = require(db, NotificationEvent, delivery.event_id)
    channel = require(db, NotificationChannel, delivery.channel_id)
    if event.type in NEWS_EVENTS and (delivery.error_code == "channel_changed"
            or news_delivery_error(db, event, channel.id)):
        raise HTTPException(409, "政策依据、规则或通道已变化，旧资讯提醒不能重新发送")
    if event.type == "notification.digest" and event.payload.get("digest", {}).get("channel_updated_at") != utc_stamp(channel.updated_at):
        raise HTTPException(409, "通道配置已变化，旧摘要不能转发到新目标；后续新事件将使用当前规则")
    if delivery.status == "unknown" and not (data and data.acknowledge_duplicate):
        raise HTTPException(409, "上次投递结果未知，重新发送可能重复；请明确确认重复风险")
    previous = delivery.status
    delivery.status, delivery.error_code, delivery.next_attempt_at = "queued", None, now()
    db.add(AuditEvent(user_id=user.id, action="notification.manual_retry", target=str(delivery.id),
                      detail={"previous_status": previous, "duplicate_possible": previous == "unknown"}))
    db.commit()
    return row(delivery)
