import base64
import hashlib
import hmac
import json
import smtplib
import socket
import ssl
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import func, select

from app.config import get_settings
from app.notification_models import NotificationChannel, NotificationDelivery, NotificationEvent, NotificationRule
from app.notifications import (
    DeliveryError, emit_event, encrypt_config, process_outbox, send_email, send_webhook, webhook_payload,
)
from app.models import now
from app.db import session_factory
from app.notifications import destination_address as real_destination_address


@pytest.fixture(autouse=True)
def notification_settings(engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "encryption_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setattr("app.notifications.destination_address", lambda url: "93.184.216.34")


def channel_rule(db, **rule_options):
    channel = NotificationChannel(name="测试接收器", kind="webhook", enabled=True,
                                  destination_hint="hooks.example.com", encrypted_config=encrypt_config({
                                      "url": "https://hooks.example.com/private-token", "secret": "signing-secret"}))
    db.add(channel)
    db.flush()
    rule = NotificationRule(name="变化", channel_id=channel.id,
                             event_types=["recruitment.first_seen"], enabled=True, **rule_options)
    db.add(rule)
    db.commit()
    return channel, rule


def emit(db, key="sample"):
    return emit_event(db, event_type="recruitment.first_seen", dedupe_key=key,
                      title="示例公司新观察岗位", text="上海 软件工程师", url="https://example.com/jobs/1")


def test_business_rollback_removes_outbox_and_duplicate_events_do_not_requeue(db):
    channel_rule(db)
    emit(db)
    db.rollback()
    assert db.scalar(select(func.count()).select_from(NotificationEvent)) == 0
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 0
    identifier = emit(db)
    db.commit()
    assert emit(db) is None
    db.commit()
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
    assert db.scalar(select(NotificationDelivery.event_id)) == identifier


def test_two_matching_rules_same_channel_and_cooldown(db):
    channel, _ = channel_rule(db, keywords=["上海"], cooldown_seconds=60)
    db.add(NotificationRule(name="第二条", channel_id=channel.id, enabled=True,
                             event_types=["recruitment.first_seen"], keywords=["上海"], cooldown_seconds=60))
    db.commit()
    emit(db)
    emit(db, "second")
    db.commit()
    assert db.scalar(select(func.count()).select_from(NotificationEvent)) == 2
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1


def test_public_channel_api_never_returns_secret(signed_client, db):
    response = signed_client.post("/api/v1/notification-channels", json={
        "name": "机器人", "kind": "webhook", "url": "https://hooks.example.com/private-token",
        "secret": "a-private-secret", "enabled": False,
    })
    assert response.status_code == 201
    public = signed_client.get("/api/v1/notification-channels").text
    assert "private-token" not in public and "a-private-secret" not in public
    channel = db.scalar(select(NotificationChannel))
    assert "private-token" not in channel.encrypted_config
    response = signed_client.post(f"/api/v1/notification-channels/{channel.id}/test")
    assert response.status_code == 409


def test_webhook_pins_destination_signs_and_sends_once_across_workers(db):
    channel_rule(db)
    event_id = emit(db)
    db.commit()
    received, gate = [], threading.Lock()

    def handler(request):
        with gate:
            received.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "hooks.example.com"
        assert request.extensions["sni_hostname"] == "hooks.example.com"
        timestamp = request.headers["X-HR-Timestamp"]
        signature = hmac.new(b"signing-secret", timestamp.encode() + b"." + request.content, hashlib.sha256).hexdigest()
        assert request.headers["X-HR-Signature"] == "sha256=" + signature
        assert request.headers["Idempotency-Key"] == str(event_id)
        return httpx.Response(204)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: process_outbox(transport=httpx.MockTransport(handler)), range(2)))
    assert sum(results) == 1 and len(received) == 1
    db.expire_all()
    assert db.scalar(select(NotificationDelivery)).status == "sent"


def test_timeout_is_unknown_and_not_automatically_retried(db):
    channel_rule(db)
    emit(db)
    db.commit()

    def handler(request):
        raise httpx.ReadTimeout("do not persist secret URL", request=request)

    assert process_outbox(transport=httpx.MockTransport(handler)) == 1
    assert process_outbox(transport=httpx.MockTransport(handler)) == 0
    db.expire_all()
    delivery = db.scalar(select(NotificationDelivery))
    assert delivery.status == "unknown" and delivery.error_code == "delivery_unknown"
    assert delivery.attempts == 1


def test_rate_limit_retries_with_same_event_id_but_permanent_error_stops(db):
    channel_rule(db)
    emit(db)
    db.commit()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["event_id"])
        return httpx.Response(429 if len(seen) == 1 else 400)

    transport = httpx.MockTransport(handler)
    process_outbox(transport=transport)
    db.expire_all()
    delivery = db.scalar(select(NotificationDelivery))
    assert delivery.status == "retrying"
    assert process_outbox(transport=transport) == 0
    delivery.next_attempt_at = now() - timedelta(seconds=1)
    db.commit()
    process_outbox(transport=transport)
    db.expire_all()
    assert delivery.status == "failed" and len(set(seen)) == 1 and len(seen) == 2


def test_feishu_signature_and_business_rejection(monkeypatch):
    monkeypatch.setattr("app.notifications.time.time", lambda: 12345)
    event = {"id": "event-id", "type": "test", "payload": {"title": "测试", "text": "正文"}}
    config = {"url": "https://open.feishu.cn/open-apis/bot/v2/hook/example", "secret": "abc"}
    body = webhook_payload("feishu", config, event)
    expected = base64.b64encode(hmac.new(b"12345\nabc", b"", hashlib.sha256).digest()).decode()
    assert body["timestamp"] == "12345" and body["sign"] == expected
    with pytest.raises(DeliveryError, match="feishu_rejected"):
        send_webhook("feishu", config, event, httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 19024, "msg": "failed"})))


def test_test_mode_blocks_unmocked_network():
    event = {"id": "test", "type": "test", "payload": {"title": "测试", "text": "内容"}}
    with pytest.raises(DeliveryError, match="test_network_disabled"):
        send_webhook("webhook", {"url": "https://example.com"}, event)
    with pytest.raises(DeliveryError, match="test_network_disabled"):
        send_email({"recipient": "test@example.com"}, event)


def test_smtp_uses_tls_stable_message_id_and_single_recipient(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "notify@example.com")
    captured = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            self.tls = False
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, **kwargs):
            self.tls = True
        def ehlo(self):
            pass
        def send_message(self, message, *, to_addrs):
            assert self.tls
            assert to_addrs == ["hr@example.com"]
            captured.append(message)
            return {}

    send_email({"recipient": "hr@example.com"}, {
        "id": "stable-event", "payload": {"title": "岗位变动", "text": "内容"}}, SMTP)
    assert captured[0]["To"] == "hr@example.com"
    assert captured[0]["Message-ID"] == "<stable-event@hr-intelligence.internal>"


def test_interrupted_delivery_becomes_unknown_without_network(db):
    channel_rule(db)
    emit(db)
    db.commit()
    delivery = db.scalar(select(NotificationDelivery))
    delivery.status, delivery.updated_at = "sending", now() - timedelta(minutes=10)
    db.commit()
    assert process_outbox() == 0
    db.expire_all()
    assert delivery.status == "unknown" and delivery.error_code == "worker_interrupted"


@pytest.mark.parametrize("url", ["https://other.example/private-webhook-token",
                                "https://hooks.example:private-webhook-token/path",
                                "https://[private-webhook-token]/path"])
def test_invalid_channel_requests_never_echo_credentials(signed_client, url):
    payload = {"name": "测试", "kind": "feishu", "url": url,
               "secret": "private-signing-secret"}
    response = signed_client.post("/api/v1/notification-channels", json=payload)
    assert response.status_code == 422
    assert "private-webhook-token" not in response.text
    assert "private-signing-secret" not in response.text
    assert all("input" not in error and "ctx" not in error for error in response.json()["detail"])


def test_concurrent_events_share_one_atomic_cooldown(db):
    channel_rule(db, cooldown_seconds=60)
    gate = threading.Barrier(3)

    def producer(index):
        with session_factory()() as session:
            gate.wait(timeout=5)
            emit(session, f"concurrent:{index}")
            session.commit()

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(producer, range(3)))
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(NotificationEvent)) == 3
    assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1


def test_old_sender_cannot_overwrite_explicitly_retried_result(signed_client, db):
    channel_rule(db)
    emit(db)
    db.commit()
    entered, release = threading.Event(), threading.Event()

    def delayed_response(request):
        entered.set()
        assert release.wait(5)
        return httpx.Response(503)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process_outbox, 1, httpx.MockTransport(delayed_response))
        try:
            assert entered.wait(5)
            db.expire_all()
            delivery = db.scalar(select(NotificationDelivery))
            assert delivery.status == "sending"
            delivery.updated_at = now() - timedelta(minutes=10)
            db.commit()
            process_outbox(limit=0)
            db.expire_all()
            assert delivery.status == "unknown"
            path = f"/api/v1/notification-deliveries/{delivery.id}/retry"
            assert signed_client.post(path).status_code == 409
            assert signed_client.post(path, json={"acknowledge_duplicate": True}).status_code == 202
            assert process_outbox(transport=httpx.MockTransport(lambda request: httpx.Response(204))) == 1
        finally:
            release.set()
        assert future.result(timeout=5) == 1
    db.expire_all()
    assert delivery.status == "sent" and delivery.attempts == 2 and delivery.error_code is None


@pytest.mark.parametrize("address", ["127.0.0.1", "::1", "169.254.169.254", "100.100.100.200",
                                     "fd00:ec2::254", "64:ff9b::7f00:1", "192.0.2.1", "224.0.0.1"])
def test_internal_webhook_allowlist_cannot_authorize_special_addresses(monkeypatch, address):
    monkeypatch.setattr(get_settings(), "notification_allowed_hosts", "hooks.internal")
    monkeypatch.setattr("app.notifications.socket.getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))])
    with pytest.raises(DeliveryError, match="restricted_destination"):
        real_destination_address("https://hooks.internal/webhook")


def test_authorized_private_destination_is_pinned_but_redirect_not_followed(monkeypatch):
    monkeypatch.setattr(get_settings(), "notification_allowed_hosts", "hooks.internal")
    monkeypatch.setattr("app.notifications.socket.getaddrinfo", lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.24.2.8", 443))])
    monkeypatch.setattr("app.notifications.destination_address", real_destination_address)
    received = []

    def redirect(request):
        received.append(request)
        assert request.url.host == "10.24.2.8"
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(DeliveryError, match="receiver_rejected"):
        send_webhook("webhook", {"url": "https://hooks.internal/webhook"}, {
            "id": "fixture", "type": "test", "payload": {"title": "测试", "text": "正文"}},
            httpx.MockTransport(redirect))
    assert len(received) == 1


def test_smtp_tls_failure_never_authenticates_or_sends(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "notify@example.com")
    monkeypatch.setattr(settings, "smtp_username", "private-user")
    actions = []

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, **kwargs):
            actions.append("tls")
            raise ssl.SSLCertVerificationError("invalid certificate")
        def login(self, *args):
            actions.append("login")
        def send_message(self, *args, **kwargs):
            actions.append("send")

    with pytest.raises(DeliveryError) as caught:
        send_email({"recipient": "hr@example.com"}, {
            "id": "fixture", "payload": {"title": "测试", "text": "正文"}}, SMTP)
    assert caught.value.code == "smtp_tls_or_auth_unsupported"
    assert not caught.value.retryable and not caught.value.ambiguous
    assert actions == ["tls"]


@pytest.mark.parametrize("code, retryable", [(450, True), (550, False)])
def test_smtp_explicit_recipient_refusal_is_not_unknown(monkeypatch, code, retryable):
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "smtp_from", "notify@example.com")

    class SMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def starttls(self, **kwargs):
            pass
        def ehlo(self):
            pass
        def send_message(self, message, *, to_addrs):
            assert to_addrs == ["hr@example.com"]
            raise smtplib.SMTPRecipientsRefused({"hr@example.com": (code, b"rejected")})

    with pytest.raises(DeliveryError) as caught:
        send_email({"recipient": "hr@example.com"}, {
            "id": "fixture", "payload": {"title": "测试", "text": "正文"}}, SMTP)
    assert caught.value.code == "smtp_recipient_rejected"
    assert caught.value.retryable is retryable and not caught.value.ambiguous


@pytest.mark.parametrize("recipient", ["one@example.com,two@example.com", "a(comment)@example.com", "a@example.com\r\nBcc: b@example.com"])
def test_saved_email_config_cannot_inject_extra_recipients(monkeypatch, recipient):
    monkeypatch.setattr(get_settings(), "smtp_host", "smtp.example.com")
    monkeypatch.setattr(get_settings(), "smtp_from", "notify@example.com")
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid recipient must be rejected before any connection")
    with pytest.raises(DeliveryError, match="invalid_recipient"):
        send_email({"recipient": recipient}, {
            "id": "fixture", "payload": {"title": "测试", "text": "正文"}}, forbidden)
