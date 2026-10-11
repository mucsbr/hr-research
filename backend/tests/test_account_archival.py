import uuid

from app.models import SourceAccount


def test_account_archival_preserves_new_unbound_accounts_and_history(signed_client, db):
    active = signed_client.post("/api/v1/source-accounts", json={"kind": "boss", "name": "真实待登录记录"}).json()
    archived = SourceAccount(kind="xhs", name="历史未登录夹具", archived=True)
    db.add(archived)
    db.commit()
    rows = signed_client.get("/api/v1/source-accounts").json()
    assert [r["id"] for r in rows] == [active["id"]]
    history = signed_client.get("/api/v1/source-accounts?include_archived=true").json()
    assert {r["id"] for r in history} == {active["id"], str(archived.id)}
    assert db.get(SourceAccount, uuid.UUID(active["id"])).archived is False
    assert db.get(SourceAccount, archived.id).health == "logged_out"


def test_archived_account_cannot_open_browser_or_bind_probe(signed_client, db, monkeypatch):
    from app.browser_api import login_manager

    archived = SourceAccount(kind="xhs", name="历史账号", archived=True)
    db.add(archived)
    db.commit()
    monkeypatch.setattr(login_manager, "start", lambda *args: (_ for _ in ()).throw(AssertionError("Browser must not open")))
    assert signed_client.post(f"/api/v1/source-accounts/{archived.id}/login-sessions").status_code == 409
    assert signed_client.put(f"/api/v1/probes/accounts/{archived.id}", json={"profile_id": None}).status_code == 409
