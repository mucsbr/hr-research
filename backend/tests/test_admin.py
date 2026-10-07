from sqlalchemy import select

from app.models import User, UserSession
from app.security import new_session


def test_only_admin_can_manage_users_and_last_admin_is_protected(signed_client, db):
    admin = db.scalar(select(User).where(User.role == "admin"))
    response = signed_client.patch(f"/api/v1/users/{admin.id}", json={"active": False})
    assert response.status_code == 409
    response = signed_client.post("/api/v1/users", json={
        "username": "reader", "password": "unit-test-password-for-reader", "role": "user",
    })
    assert response.status_code == 201
    assert "password" not in response.text
    user = db.scalar(select(User).where(User.username == "reader"))
    token = new_session(db, user)
    signed_client.cookies.set("hr_session", token)
    assert signed_client.get("/api/v1/users").status_code == 403
    assert signed_client.get("/api/v1/companies").status_code == 200


def test_deactivation_revokes_existing_sessions(signed_client, db):
    response = signed_client.post("/api/v1/users", json={
        "username": "staff", "password": "unit-test-staff-password", "role": "user",
    })
    user = db.scalar(select(User).where(User.username == "staff"))
    new_session(db, user)
    assert signed_client.patch('/api/v1/users/' + response.json()["id"], json={"active": False}).status_code == 200
    assert not db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()


def test_invalid_sensitive_input_is_not_reflected_in_error(client):
    secret = "secret-sample-not-a-real-credential-" * 20
    response = client.post("/api/v1/auth/login", json={"username": "admin", "password": secret})
    assert response.status_code == 422
    assert secret not in response.text and "input" not in response.json()["detail"][0]
