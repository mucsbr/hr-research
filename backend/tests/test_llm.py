import json

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.llm import ModelError, ModelGateway
from app.models import ModelCall, TaskRun


def reply(request):
    body = json.loads(request.content)
    assert body["model"] == "glm-5.3"
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] <= get_settings().llm_max_output_tokens
    return httpx.Response(200, json={
        "choices": [{"message": {"content": '{"skills":["Python"]}'}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 8},
    })


def test_nonstream_records_usage_without_live_model(db):
    response = ModelGateway(httpx.MockTransport(reply)).complete(
        [{"role": "user", "content": "Extract skills from Python development"}], purpose="unit_test"
    )
    assert response.content == '{"skills":["Python"]}'
    call = db.scalar(select(ModelCall))
    assert call.status == "completed" and call.completion_tokens == 8


def test_stream_usage_chunk_can_have_no_choices(db):
    def stream_reply(request):
        assert json.loads(request.content)["stream_options"]["include_usage"]
        chunks = [
            {"choices": [{"delta": {"content": "岗位"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 2}},
        ]
        content = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, text=content + "data: [DONE]\n\n",
                              headers={"Content-Type": "text/event-stream"})
    response = ModelGateway(httpx.MockTransport(stream_reply)).complete(
        [{"role": "user", "content": "test"}], purpose="unit_test", stream=True
    )
    assert response.content == "岗位" and response.usage["completion_tokens"] == 2


def test_daily_budget_stops_before_network(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_daily_request_limit", 1)
    gateway = ModelGateway(httpx.MockTransport(reply))
    gateway.complete([{"role": "user", "content": "one"}], purpose="unit_test")
    with pytest.raises(ModelError, match="今日模型请求预算"):
        gateway.complete([{"role": "user", "content": "two"}], purpose="unit_test")
    assert len(db.scalars(select(ModelCall)).all()) == 1


def test_timeout_is_counted_and_never_retried(db):
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout")
    with pytest.raises(ModelError):
        ModelGateway(httpx.MockTransport(timeout)).complete(
            [{"role": "user", "content": "one"}], purpose="unit_test"
        )
    assert len(calls) == 1
    assert db.scalar(select(ModelCall)).status == "unknown"


def test_input_limit_stops_before_reservation(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_max_input_chars", 2)
    with pytest.raises(ModelError, match="输入超过"):
        ModelGateway(httpx.MockTransport(reply)).complete(
            [{"role": "user", "content": "too long"}], purpose="unit_test"
        )
    assert not db.scalars(select(ModelCall)).all()


def test_automated_tests_cannot_call_live_model(db):
    with pytest.raises(ModelError, match="禁止连接真实模型"):
        ModelGateway().complete([{"role": "user", "content": "one"}], purpose="unit_test")
    assert not db.scalars(select(ModelCall)).all()


def test_task_budget_stops_before_network(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_task_call_limit", 1)
    task = TaskRun(kind="test", domain="test")
    db.add(task)
    db.commit()
    gateway = ModelGateway(httpx.MockTransport(reply))
    gateway.complete([{"role": "user", "content": "one"}], purpose="unit_test", task_id=task.id)
    with pytest.raises(ModelError, match="本任务模型调用预算"):
        gateway.complete([{"role": "user", "content": "two"}], purpose="unit_test", task_id=task.id)
    assert len(db.scalars(select(ModelCall)).all()) == 1


def test_concurrent_reservation_does_not_overspend(db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(get_settings(), "llm_daily_request_limit", 1)
    def invoke(_):
        try:
            ModelGateway(httpx.MockTransport(reply)).complete(
                [{"role": "user", "content": "one"}], purpose="unit_test"
            )
            return "completed"
        except ModelError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert sorted(results) == ["completed", "daily_budget"]
    assert len(db.scalars(select(ModelCall)).all()) == 1


def test_reasoning_budget_exhaustion_is_classified_and_metered(db):
    def truncated(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 40, "completion_tokens": 100,
                      "completion_tokens_details": {"reasoning_tokens": 100}},
        })
    with pytest.raises(ModelError, match="含推理"):
        ModelGateway(httpx.MockTransport(truncated)).complete(
            [{"role": "user", "content": "one"}], purpose="unit_test", max_output_tokens=100
        )
    call = db.scalar(select(ModelCall))
    assert call.error_code == "output_truncated"
    assert call.reasoning_tokens == call.completion_tokens == 100
