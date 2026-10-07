from sqlalchemy import select

from app.models import CompanyRun, TaskRun


def company(client, name="测试公司"):
    response = client.post("/api/v1/companies", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def test_private_routes_require_login(client):
    assert client.get("/api/v1/companies").status_code == 401
    assert client.get("/api/v1/health").status_code == 200


def test_logout_revokes_session(signed_client):
    assert signed_client.get("/api/v1/auth/me").status_code == 200
    token = signed_client.cookies.get("hr_session")
    assert signed_client.post("/api/v1/auth/logout").status_code == 200
    signed_client.cookies.set("hr_session", token)
    assert signed_client.get("/api/v1/auth/me").status_code == 401


def test_mutation_rejects_foreign_origin(signed_client):
    assert signed_client.post("/api/v1/companies", headers={"Origin": "https://evil.invalid"},
                              json={"name": "不应创建"}).status_code == 403


def test_companies_are_not_merged_by_name(signed_client):
    a, b = company(signed_client), company(signed_client)
    assert a["id"] != b["id"]
    assert len(signed_client.get("/api/v1/companies").json()) == 2


def test_batch_splits_company_context_and_is_idempotent(signed_client, db):
    a, b = company(signed_client, "甲"), company(signed_client, "乙")
    payload = {"company_ids": [a["id"], b["id"], a["id"]], "idempotency_key": "test-once"}
    first = signed_client.post("/api/v1/recruitment/runs", json=payload)
    second = signed_client.post("/api/v1/recruitment/runs", json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["batch_id"] == second.json()["batch_id"]
    assert second.json()["created"] is False
    tasks = db.scalars(select(TaskRun)).all()
    assert len(tasks) == 2
    assert {t.config_snapshot["company_name"] for t in tasks} == {"甲", "乙"}
    assert all(t.status == "blocked_configuration" for t in tasks)
    assert len(db.scalars(select(CompanyRun)).all()) == 2


def test_source_uniqueness_and_url_validation(signed_client):
    a = company(signed_client)
    payload = {"company_id": a["id"], "kind": "careers", "entry_url": "https://example.com/jobs"}
    assert signed_client.post("/api/v1/source-bindings", json=payload).status_code == 201
    assert signed_client.post("/api/v1/source-bindings", json=payload).status_code == 409
    payload["entry_url"] = "file:///etc/passwd"
    assert signed_client.post("/api/v1/source-bindings", json=payload).status_code == 422


def test_schedule_requires_valid_expression(signed_client):
    a = company(signed_client)
    response = signed_client.post("/api/v1/plans", json={
        "name": "定时测试", "company_ids": [a["id"]], "enabled": True, "schedule": "invalid",
    })
    assert response.status_code == 422
    response = signed_client.post("/api/v1/plans", json={
        "name": "定时测试", "company_ids": [a["id"]], "enabled": True, "schedule": "0 9 * * *",
    })
    assert response.status_code == 201
    assert response.json()["next_run_at"]


def test_config_response_never_contains_credentials(signed_client):
    from app.config import get_settings
    response = signed_client.get("/api/v1/system/status")
    assert response.status_code == 200
    assert get_settings().llm_api_key.get_secret_value() not in response.text
    assert get_settings().admin_password.get_secret_value() not in response.text
