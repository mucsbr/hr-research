"""Transactional outbox; no network calls from business transactions."""
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import smtplib
import socket
import ssl
import time
import uuid
from datetime import timedelta
from email.message import EmailMessage
from email.headerregistry import Address
from email.errors import HeaderParseError
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, text as sql_text, update
from sqlalchemy.dialects.postgresql import insert

from app.browser_sessions import cipher
from app.collection import CollectionError, validate_fetch_url
from app.config import get_settings
from app.db import session_factory
from app.models import now
from app.notification_models import (
    NotificationChannel, NotificationDelivery, NotificationEvent, NotificationRule,
)

EVENT_TYPES = {
    "recruitment.first_seen", "recruitment.content_changed", "recruitment.salary_changed",
    "recruitment.suspected_removed", "recruitment.reappeared", "source.attention",
    "sentiment.negative", "sentiment.new", "news.new", "news.policy", "news.policy_effective", "report.ready",
}
NEWS_EVENTS = {"news.new", "news.policy", "news.policy_effective"}
NEWS_FILTER_KEYS = ("regions", "industries", "topics")


def lock_notification_state(db):
    """Short transaction gate; never held while contacting a receiver.

    News acquires this before news-ingest. Other domains may hold task/ingest
    locks already; notification operations never acquire those in reverse.
    """
    db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
               {"key": "notifications-state-v1"})


def normalized_tags(values):
    return sorted({value.strip().casefold() for value in values or [] if isinstance(value, str) and value.strip()})


def rule_matches(rule, *, event_type, subject_id, title, text, news=None):
    if not rule.enabled or event_type not in rule.event_types:
        return False
    if event_type == "news.policy_effective" and rule.delivery_mode != "immediate":
        return False
    if rule.subject_ids and str(subject_id) not in rule.subject_ids:
        return False
    if rule.keywords and not any(word.casefold() in (title + " " + text).casefold() for word in rule.keywords):
        return False
    for key in NEWS_FILTER_KEYS:
        wanted = set(normalized_tags((rule.news_filters or {}).get(key)))
        if wanted and (event_type not in NEWS_EVENTS or not wanted.intersection(normalized_tags((news or {}).get(key)))):
            return False
    return True


def encrypt_config(config):
    return cipher().encrypt(json.dumps(config, ensure_ascii=False).encode()).decode()


def decrypt_config(value):
    return json.loads(cipher().decrypt(value.encode()))


def apply_rule(db, rule, channel, event_id, *, recorded_at):
    if rule.delivery_mode == "daily_digest":
        from app.notification_digest import capture_digest_item
        capture_digest_item(db, rule, channel, event_id, recorded_at=recorded_at)
        return
    if rule.last_queued_at and rule.last_queued_at + timedelta(seconds=rule.cooldown_seconds) > recorded_at:
        return
    db.execute(insert(NotificationDelivery).values(
        id=uuid.uuid4(), event_id=event_id, channel_id=channel.id, next_attempt_at=recorded_at,
    ).on_conflict_do_nothing(index_elements=["event_id", "channel_id"]))
    rule.last_queued_at = recorded_at


def emit_event(db, *, event_type, dedupe_key, title, text, subject_id=None, task_id=None, url="",
               news=None, policy=None, recorded_at=None):
    """Call before the enclosing business commit, using a stable domain-event key."""
    if event_type not in EVENT_TYPES:
        raise ValueError("Unsupported notification event")
    lock_notification_state(db)
    stamp = recorded_at or now()
    payload = {"title": str(title)[:250], "text": str(text)[:3000], "url": str(url)[:2000]}
    if news is not None:
        if event_type not in NEWS_EVENTS:
            raise ValueError("News metadata requires a news event")
        payload["news"] = dict(news)
    if policy is not None:
        if event_type != "news.policy_effective":
            raise ValueError("Policy metadata requires a policy reminder event")
        payload["policy"] = dict(policy)
    event_id = db.execute(insert(NotificationEvent).values(
        id=uuid.uuid4(), type=event_type, dedupe_key=dedupe_key,
        subject_id=subject_id, task_id=task_id, payload=payload, created_at=stamp, updated_at=stamp,
    ).on_conflict_do_nothing(index_elements=["dedupe_key"]).returning(NotificationEvent.id)).scalar()
    if not event_id:
        return None
    # Lock in stable order so cooldown is applied atomically by competing sources.
    rules = db.scalars(select(NotificationRule).where(NotificationRule.enabled.is_(True))
                       .order_by(NotificationRule.id).with_for_update()
                       .execution_options(populate_existing=True)).all()
    for rule in rules:
        if not rule_matches(rule, event_type=event_type, subject_id=subject_id,
                title=payload["title"] if event_type in NEWS_EVENTS else title,
                text=payload["text"] if event_type in NEWS_EVENTS else text, news=news):
            continue
        channel = db.get(NotificationChannel, rule.channel_id, populate_existing=True)
        if not channel or not channel.enabled:
            continue
        apply_rule(db, rule, channel, event_id, recorded_at=stamp)
    return event_id


def news_delivery_error(db, event, channel_id):
    """Called under notification gate before claim/retry; no network calls."""
    if event.type not in NEWS_EVENTS:
        return None
    if event.type == "news.policy_effective":
        from app.news_policy_reminders import policy_event_error
        error = policy_event_error(db, event)
        if error:
            return error
    rules = db.scalars(select(NotificationRule).where(NotificationRule.channel_id == channel_id,
        NotificationRule.enabled.is_(True), NotificationRule.delivery_mode == "immediate")
        .execution_options(populate_existing=True)).all()
    if not any(rule_matches(rule, event_type=event.type, subject_id=event.subject_id,
            title=event.payload.get("title", ""), text=event.payload.get("text", ""),
            news=event.payload.get("news")) for rule in rules):
        return "news_rule_changed"
    return None


def reconcile_news_deliveries(db, channel_ids, *, limit=500):
    lock_notification_state(db)
    rows = db.execute(select(NotificationDelivery, NotificationEvent).join(NotificationEvent,
        NotificationDelivery.event_id == NotificationEvent.id).where(
            NotificationDelivery.channel_id.in_(set(channel_ids)),
            NotificationDelivery.status.in_(["queued", "retrying"]), NotificationEvent.type.in_(NEWS_EVENTS))
        .order_by(NotificationDelivery.id).with_for_update(of=NotificationDelivery).limit(limit)).all()
    cancelled = 0
    for delivery, event in rows:
        error = news_delivery_error(db, event, delivery.channel_id)
        if error:
            delivery.status, delivery.error_code = "cancelled", error
            cancelled += 1
    return cancelled


class DeliveryError(Exception):
    def __init__(self, code, *, retryable=False, ambiguous=False):
        self.code, self.retryable, self.ambiguous = code, retryable, ambiguous
        super().__init__(code)


def destination_address(url):
    parsed = urlparse(url)
    if not parsed.hostname or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise DeliveryError("invalid_destination")
    settings = get_settings()
    allowed = {host.strip().lower() for host in settings.notification_allowed_hosts.split(",") if host.strip()}
    if parsed.hostname in allowed:
        # A deployment administrator can authorize an exact internal webhook host.
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        except socket.gaierror as exc:
            raise DeliveryError("destination_dns_failed", retryable=exc.errno == socket.EAI_AGAIN) from exc
        ips = sorted({item[4][0] for item in addresses})
        # Exact-host authorization permits RFC1918 / ULA business services, never
        # loopback, metadata, multicast, documentation or other special networks.
        private_networks = tuple(ipaddress.ip_network(value) for value in (
            "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
        def allowed_address(value):
            ip = ipaddress.ip_address(value)
            if ip == ipaddress.ip_address("fd00:ec2::254"):
                return False  # AWS IMDS IPv6 endpoint is inside the ULA range.
            return (ip.is_global and not ip.is_multicast and not ip.is_reserved
                    or any(ip in network for network in private_networks))
        if not ips or not all(allowed_address(ip) for ip in ips):
            raise DeliveryError("restricted_destination")
        return ips[0]
    if parsed.scheme != "https" and not settings.allow_local_fixture_fetch:
        raise DeliveryError("https_required")
    try:
        return validate_fetch_url(url)
    except CollectionError as exc:
        raise DeliveryError(exc.code) from exc


def webhook_payload(kind, config, event):
    payload = event["payload"]
    if kind == "webhook":
        return {"schema_version": 1, "event_id": event["id"], "event_type": event["type"], **payload}
    body = {"msg_type": "text", "content": {"text":
        f"{payload['title']}\n{payload['text']}\n{payload.get('url', '')}\n事件：{event['id']}"}}
    if config.get("secret"):
        stamp = str(int(time.time()))
        signature_key = f"{stamp}\n{config['secret']}".encode()
        body.update(timestamp=stamp, sign=base64.b64encode(
            hmac.new(signature_key, b"", hashlib.sha256).digest()).decode())
    return body


def validate_recipient(value):
    """One bare mailbox, checked again at delivery for saved/legacy configurations."""
    if not isinstance(value, str) or len(value) > 250 or not re.fullmatch(
        r"[^\s@<>,;]+@[^\s@<>,;]+\.[^\s@<>,;]+", value
    ):
        raise DeliveryError("invalid_recipient")
    try:
        address = Address(addr_spec=value)
    except (ValueError, IndexError, HeaderParseError):
        raise DeliveryError("invalid_recipient") from None
    if address.addr_spec != value:
        raise DeliveryError("invalid_recipient")
    return value


def send_webhook(kind, config, event, transport=None):
    if get_settings().app_env == "test" and transport is None:
        raise DeliveryError("test_network_disabled")
    url = config["url"]
    address = destination_address(url)
    parsed = urlparse(url)
    body = webhook_payload(kind, config, event)
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Host": parsed.netloc, "Content-Type": "application/json", "Accept-Encoding": "identity",
               "Idempotency-Key": event["id"], "X-Event-ID": event["id"]}
    if kind == "webhook" and config.get("secret"):
        stamp = str(int(time.time()))
        signature = hmac.new(config["secret"].encode(), stamp.encode() + b"." + encoded,
                             hashlib.sha256).hexdigest()
        headers.update({"X-HR-Timestamp": stamp, "X-HR-Signature": "sha256=" + signature})
    try:
        with httpx.Client(timeout=get_settings().notification_timeout_seconds,
                          follow_redirects=False, trust_env=False, transport=transport) as client:
            with client.stream("POST", httpx.URL(url).copy_with(host=address),
                               content=encoded, headers=headers,
                               extensions={"sni_hostname": parsed.hostname}) as response:
                if response.status_code == 429:
                    raise DeliveryError("rate_limited", retryable=True)
                if response.status_code >= 500:
                    raise DeliveryError("receiver_error", ambiguous=True)
                if not 200 <= response.status_code < 300:
                    raise DeliveryError("receiver_rejected")
                raw = b""
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    raw += chunk
                    if len(raw) > 65536:
                        raise DeliveryError("response_too_large", ambiguous=True)
                if kind == "feishu":
                    try:
                        result = json.loads(raw)
                        code = result.get("code", result.get("StatusCode"))
                    except (ValueError, AttributeError):
                        raise DeliveryError("invalid_receiver_response", ambiguous=True) from None
                    if code != 0:
                        raise DeliveryError("feishu_rejected")
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        raise DeliveryError("connection_failed", retryable=True) from exc
    except httpx.HTTPError as exc:
        # A write/read failure could have happened after the receiver accepted it.
        raise DeliveryError("delivery_unknown", ambiguous=True) from exc


def send_email(config, event, smtp_factory=None):
    settings = get_settings()
    if settings.app_env == "test" and smtp_factory is None:
        raise DeliveryError("test_network_disabled")
    if not settings.smtp_host or not settings.smtp_from:
        raise DeliveryError("smtp_not_configured")
    recipient = validate_recipient(config.get("recipient"))
    message = EmailMessage()
    message["Subject"] = event["payload"]["title"].replace("\n", " ").replace("\r", " ")
    message["From"], message["To"] = settings.smtp_from, recipient
    message["Message-ID"] = f"<{event['id']}@hr-intelligence.internal>"
    message.set_content(event["payload"]["text"] + "\n" + event["payload"].get("url", ""))
    factory = smtp_factory or (smtplib.SMTP_SSL if settings.smtp_security == "ssl" else smtplib.SMTP)
    sending, accepted = False, False
    try:
        kwargs = {"timeout": settings.notification_timeout_seconds}
        if settings.smtp_security == "ssl":
            kwargs["context"] = ssl.create_default_context()
        with factory(settings.smtp_host, settings.smtp_port, **kwargs) as client:
            if settings.smtp_security == "starttls":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            sending = True
            rejected = client.send_message(message, to_addrs=[recipient])
            if rejected:
                raise DeliveryError("smtp_recipient_rejected", retryable=all(
                    400 <= code < 500 for code, _ in rejected.values()))
            accepted = True
    except DeliveryError:
        raise
    except smtplib.SMTPRecipientsRefused as exc:
        raise DeliveryError("smtp_recipient_rejected", retryable=bool(exc.recipients) and all(
            400 <= code < 500 for code, _ in exc.recipients.values())) from exc
    except (ssl.SSLError, smtplib.SMTPNotSupportedError) as exc:
        if accepted:
            return
        raise DeliveryError("smtp_unknown" if sending else "smtp_tls_or_auth_unsupported",
                            ambiguous=sending) from exc
    except smtplib.SMTPResponseException as exc:
        if accepted:
            return
        raise DeliveryError("smtp_rejected", retryable=400 <= exc.smtp_code < 500) from exc
    except (OSError, smtplib.SMTPException) as exc:
        if accepted:
            return
        raise DeliveryError("smtp_unknown" if sending else "smtp_connection_failed",
                            retryable=not sending, ambiguous=sending) from exc


def process_outbox(limit=20, transport=None, smtp_factory=None):
    # A crashed in-flight send is ambiguous; never silently redeliver it.
    with session_factory()() as db:
        lock_notification_state(db)
        db.execute(update(NotificationDelivery).where(
            NotificationDelivery.status == "sending",
            NotificationDelivery.updated_at < now() - timedelta(minutes=5),
        ).values(status="unknown", error_code="worker_interrupted"))
        db.commit()
    processed = 0
    for _ in range(limit):
        with session_factory()() as db:
            lock_notification_state(db)
            delivery = db.scalar(select(NotificationDelivery).where(
                NotificationDelivery.status.in_(["queued", "retrying"]),
                NotificationDelivery.next_attempt_at <= now(),
            ).order_by(NotificationDelivery.created_at)
                .with_for_update(skip_locked=True).limit(1))
            if not delivery:
                break
            channel = db.get(NotificationChannel, delivery.channel_id)
            if not channel.enabled:
                delivery.status, delivery.error_code = "cancelled", "channel_disabled"
                db.commit()
                continue
            event_row = db.get(NotificationEvent, delivery.event_id)
            error = news_delivery_error(db, event_row, channel.id)
            if error:
                delivery.status, delivery.error_code = "cancelled", error
                db.commit()
                continue
            if event_row.type == "notification.digest":
                from app.notification_digest import utc_stamp
                digest = event_row.payload.get("digest", {})
                if digest.get("channel_updated_at") != utc_stamp(channel.updated_at):
                    delivery.status, delivery.error_code = "cancelled", "channel_changed"
                    db.commit()
                    continue
            event = {"id": str(event_row.id), "type": event_row.type, "payload": event_row.payload}
            identifier, kind, encrypted = delivery.id, channel.kind, channel.encrypted_config
            delivery.status, delivery.attempts = "sending", delivery.attempts + 1
            delivery.updated_at = now()
            attempts = delivery.attempts
            db.commit()
        status, error, prepared = "sent", None, False
        try:
            config = decrypt_config(encrypted)
            prepared = True
            if kind == "email":
                send_email(config, event, smtp_factory)
            else:
                send_webhook(kind, config, event, transport)
        except DeliveryError as exc:
            error = exc.code
            status = "unknown" if exc.ambiguous else "retrying" if exc.retryable and attempts < 3 else "failed"
        except Exception:
            status, error = ("unknown", "unexpected_delivery_error") if prepared else ("failed", "configuration_error")
        with session_factory()() as db:
            # A stale sender must not overwrite interruption recovery or a newer
            # explicitly requested attempt while it was outside the transaction.
            db.execute(update(NotificationDelivery).where(
                NotificationDelivery.id == identifier,
                NotificationDelivery.status == "sending",
                NotificationDelivery.attempts == attempts,
            ).values(status=status, error_code=error, sent_at=now() if status == "sent" else None,
                     next_attempt_at=now() + timedelta(seconds=min(60 * 2 ** min(attempts - 1, 4), 600))))
            db.commit()
        processed += 1
    return processed
