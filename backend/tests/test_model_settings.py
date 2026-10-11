import json
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.llm import ModelError, ModelGateway
from app.model_settings import ModelConfiguration, get_model_settings
from app.models import AuditEvent, ModelCall, TaskRun, User, now
from app.security import new_session

SECRET = "isolated-model-key-not-real"


def configuration(**changes):
    return {"base_url": "https://custom.example.invalid/api/v1", "api_key": SECRET,
            "model": "my-provider/deployment:2026", "fallback_model": "", "enabled": True,
            "daily_request_limit": 12, "task_call_limit": 3, "max_output_tokens": 1200,
            "max_input_chars": 16000, "timeout_seconds": 45, "reasoning_effort": None,
            "output_token_parameter": "max_tokens", "expected_revision": 0, **changes}


def save(client, **changes):
    response = client.put("/api/v1/settings/models", json=configuration(**changes))
    assert response.status_code == 200, response.text
    return response.json()


def test_custom_model_configuration_encrypts_key_and_preserves_deployment_before_save(signed_client, db):
    initial = signed_client.get("/api/v1/settings/models").json()
    assert initial["origin"] == "deployment" and initial["model"] == get_settings().llm_model
    result = save(signed_client, base_url="https://custom.example.invalid/api/v1/chat/completions/")
    assert result["model"] == "my-provider/deployment:2026"
    assert result["base_url"] == "https://custom.example.invalid/api/v1"
    assert result["has_api_key"] and result["revision"] == 1
    assert SECRET not in json.dumps(result) and "api_key" not in result
    record = db.scalar(select(ModelConfiguration))
    assert record.encrypted_api_key and SECRET not in record.encrypted_api_key
    assert SECRET not in json.dumps(record.parameters)
    assert SECRET not in json.dumps(db.scalar(select(AuditEvent)).detail)
    assert not db.scalars(select(ModelCall)).all()


def test_gateway_uses_saved_url_key_and_free_model_name_without_restart(signed_client, db):
    requests = []
    def answer(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Mock result"}, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
    gateway = ModelGateway(httpx.MockTransport(answer))
    save(signed_client)
    gateway.complete([{"role": "user", "content": "test"}], purpose="test")
    assert str(requests[0].url) == "https://custom.example.invalid/api/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer " + SECRET
    assert json.loads(requests[0].content)["model"] == "my-provider/deployment:2026"
    assert "reasoning_effort" not in json.loads(requests[0].content)
    save(signed_client, model="another-model-any-name", expected_revision=1, api_key="")
    gateway.complete([{"role": "user", "content": "test2"}], purpose="test")
    assert json.loads(requests[1].content)["model"] == "another-model-any-name"
    assert requests[1].headers["Authorization"] == "Bearer " + SECRET
    assert {c.model for c in db.scalars(select(ModelCall))} == {"my-provider/deployment:2026", "another-model-any-name"}


def test_model_change_does_not_reset_paid_intents_or_daily_budget(signed_client, db):
    prior = ModelCall(purpose="previous-provider", model="previous", input_chars=10, max_output_tokens=5, status="unknown")
    db.add(prior)
    db.commit()
    save(signed_client, daily_request_limit=1)
    settings = signed_client.get("/api/v1/system/status").json()["llm"]
    assert settings["daily_request_limit"] == 1 and settings["calls_today"] == 1
    requests = []
    with pytest.raises(ModelError, match="今日模型请求预算"):
        ModelGateway(httpx.MockTransport(lambda request: requests.append(request))).complete(
            [{"role": "user", "content": "test"}], purpose="test")
    assert not requests and db.scalar(select(ModelCall)).id == prior.id


def test_key_retention_replacement_clear_and_url_change_requires_explicit_new_key(signed_client, db):
    save(signed_client)
    response = signed_client.put("/api/v1/settings/models", json=configuration(base_url="https://other.invalid/v1", api_key="", expected_revision=1))
    assert response.status_code == 422
    assert get_model_settings().llm_base_url == "https://custom.example.invalid/api/v1"
    save(signed_client, base_url="https://other.invalid/v1", api_key="replacement-not-real", expected_revision=1)
    assert get_model_settings().llm_api_key.get_secret_value() == "replacement-not-real"
    result = save(signed_client, base_url="https://other.invalid/v1", api_key="", clear_api_key=True, enabled=False, expected_revision=2)
    assert not result["has_api_key"] and not result["configured"]
    assert not db.scalar(select(ModelConfiguration)).encrypted_api_key


def test_disabled_configuration_does_not_fall_back_to_environment_key(signed_client, db):
    save(signed_client, enabled=False)
    result = signed_client.get("/api/v1/settings/models").json()
    assert result["has_api_key"] and not result["configured"]
    with pytest.raises(ModelError, match="模型连接未配置"):
        ModelGateway(httpx.MockTransport(lambda request: pytest.fail("No network"))).complete(
            [{"role": "user", "content": "test"}], purpose="test")
    assert not db.scalars(select(ModelCall)).all()


def test_model_configuration_is_admin_only_and_conflicts_do_not_overwrite(signed_client, db):
    save(signed_client)
    assert signed_client.put("/api/v1/settings/models", json=configuration(model="stale-write")).status_code == 409
    assert get_model_settings().llm_model == "my-provider/deployment:2026"
    user = User(username="reader-model", password_hash="not-used", role="user")
    db.add(user)
    db.commit()
    signed_client.cookies.set("hr_session", new_session(db, user))
    assert signed_client.get("/api/v1/settings/models").status_code == 403
    assert signed_client.put("/api/v1/settings/models", json=configuration()).status_code == 403
    assert signed_client.get("/api/v1/system/status").json()["llm"]["model"] == "my-provider/deployment:2026"


@pytest.mark.parametrize("base", ["file:///secret", "https://key:secret@provider.invalid/v1", "https://provider.invalid/v1?key=secret", "https://provider.invalid/v1#token", "https://provider.invalid:0/v1"])
def test_invalid_endpoint_never_reserves_or_reflects_key(signed_client, db, base):
    response = signed_client.put("/api/v1/settings/models", json=configuration(base_url=base))
    assert response.status_code == 422 and SECRET not in response.text
    assert not db.scalars(select(ModelConfiguration)).all()
    assert not db.scalars(select(ModelCall)).all()


def test_optional_parameters_and_internal_compatible_endpoint(signed_client, db):
    save(signed_client, base_url="http://10.1.2.3:8000/v1", model="my-reasoning-model",
         reasoning_effort="medium", output_token_parameter="max_completion_tokens")
    def answer(request):
        payload = json.loads(request.content)
        assert payload["reasoning_effort"] == "medium"
        assert payload["max_completion_tokens"] == 1200 and "max_tokens" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": "Mock"}, "finish_reason": "stop"}]})
    ModelGateway(httpx.MockTransport(answer)).complete([{"role": "user", "content": "test"}], purpose="test")


def test_corrupt_secret_does_not_use_environment_or_make_request(signed_client, db):
    save(signed_client)
    record = db.scalar(select(ModelConfiguration))
    record.encrypted_api_key = "damaged"
    db.commit()
    assert signed_client.get("/api/v1/settings/models").status_code == 503
    with pytest.raises(ModelError, match="密钥无法解密"):
        ModelGateway(httpx.MockTransport(lambda request: pytest.fail("No network"))).complete(
            [{"role": "user", "content": "test"}], purpose="test")
    assert not db.scalars(select(ModelCall)).all()


@pytest.mark.parametrize("timeout,expected", [(10, "unknown"), (240, "generating")])
def test_generation_watchdog_respects_the_platform_timeout(db, timeout, expected):
    from app.adapter_code_orchestration import tick_code_maintenance
    from test_adapter_code_orchestration import proposal_for

    proposal, *_ = proposal_for(db)
    proposal.status = "generating"
    task = db.get(TaskRun, proposal.task_id)
    task.status = "running"
    task.heartbeat_at = now() - timedelta(seconds=180)
    db.add(ModelConfiguration(parameters={"timeout_seconds": timeout, "enabled": False}))
    db.commit()
    assert tick_code_maintenance(proposal.id, client=object())["status"] == expected
    assert not db.scalars(select(ModelCall)).all()


@pytest.mark.parametrize("change,code", [({"daily_request_limit": 0}, "daily_budget"),
                                       ({"base_url": "https://new.example.invalid/v1", "api_key": "new-not-real"}, "configuration_changed")])
def test_configuration_change_before_reservation_is_checked_under_the_budget_lock(signed_client, db, monkeypatch, change, code):
    from app import llm

    save(signed_client)
    reserve = llm.reserve_call
    def changed_before_reserving(*args):
        save(signed_client, **{"expected_revision": 1, "api_key": "", **change})
        return reserve(*args)
    monkeypatch.setattr(llm, "reserve_call", changed_before_reserving)
    with pytest.raises(ModelError) as error:
        ModelGateway(httpx.MockTransport(lambda request: pytest.fail("No request before revalidation"))).complete(
            [{"role": "user", "content": "test"}], purpose="test")
    assert error.value.code == code
    assert not db.scalars(select(ModelCall)).all()
