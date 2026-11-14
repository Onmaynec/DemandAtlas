"""v0.4.0: durable key management, isolation and audit regressions."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app.auth import issue_key, utc_now, validate_runtime
from backend.app.db import SessionLocal
from backend.app.main import app
from backend.app.models import AccessAudit, AccessKey, AccessUser, Product, Store

client = TestClient(app)


def admin_identity(name="owner"):
    from uuid import uuid4
    with SessionLocal.begin() as db:
        user = AccessUser(id=str(uuid4()), name=name, role="access_admin",
                          stores=[], categories=[], enabled=True)
        db.add(user)
        db.flush()
        token, key = issue_key(db, user, "bootstrap", "offline-bootstrap")
        db.flush()
        return {"Authorization": "Bearer " + token}, user.id, key.id


def catalog():
    with SessionLocal.begin() as db:
        db.add_all([
            Store(id="S001", name="First", region="North"),
            Store(id="S002", name="Second", region="South"),
            Product(sku="FOOD", name="Food", category="Food", unit_cost=5),
            Product(sku="HOME", name="Household", category="Home", unit_cost=10),
        ])


def test_production_requires_secure_configuration(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEMO_MODE", "1")
    with pytest.raises(RuntimeError, match="DEMO_MODE"):
        validate_runtime()
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("TOKEN_ANALYST", "legacy")
    with pytest.raises(RuntimeError, match="Static demonstration token"):
        validate_runtime()
    monkeypatch.delenv("TOKEN_ANALYST")
    monkeypatch.setenv("FORECAST_SERVICE_TOKEN", "change-me-before-deployment")
    with pytest.raises(RuntimeError, match="FORECAST_SERVICE_TOKEN"):
        validate_runtime()
    monkeypatch.setenv("FORECAST_SERVICE_TOKEN", "random-strong-key-for-test")
    validate_runtime()


def test_disable_demo_tokens_create_scoped_key_and_revoke(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    catalog()
    auth, admin_id, admin_key = admin_identity()
    denied_demo = client.get("/api/v1/auth/me",
                             headers={"Authorization": "Bearer demo-analyst"})
    assert denied_demo.status_code == 401
    created = client.post("/api/v1/admin/users", headers=auth, json={
        "name": "retail_user", "role": "observer",
        "stores": ["S001"], "categories": ["Food"], "expiresInDays": 15,
    })
    assert created.status_code == 201, created.text
    data = created.json()
    assert data["name"] == "retail_user"
    assert data["tokenOnce"].startswith("da_")
    user_auth = {"Authorization": "Bearer " + data["tokenOnce"]}
    profile = client.get("/api/v1/auth/me", headers=user_auth)
    assert profile.status_code == 200, profile.text
    assert profile.json()["stores"] == ["S001"]
    assert profile.json()["categories"] == ["Food"]
    catalogs = client.get("/api/v1/catalog", headers=user_auth)
    assert catalogs.status_code == 200
    assert {s["id"] for s in catalogs.json()["stores"]} == {"S001"}
    assert {p["sku"] for p in catalogs.json()["products"]} == {"FOOD"}
    assert client.get("/api/v1/admin/users", headers=user_auth).status_code == 403
    assert client.get("/api/v1/metrics/sales?start=2026-01-01&end=2026-01-10&store=S002",
                      headers=user_auth).status_code == 403
    assert client.get("/api/v1/catalog", headers=auth).status_code == 403
    assert client.get("/api/v1/metrics/sales?start=2026-01-01&end=2026-01-10",
                      headers=auth).status_code == 403
    with SessionLocal() as db:
        record = db.get(AccessKey, data["key"]["id"])
        assert record.token_hash != data["tokenOnce"]
        assert len(record.token_hash) == 64
    all_users = client.get("/api/v1/admin/users", headers=auth)
    assert all_users.status_code == 200
    assert "tokenOnce" not in all_users.text
    revoked = client.post(f"/api/v1/admin/keys/{data['key']['id']}/revoke",
                          headers=auth, json={})
    assert revoked.status_code == 200, revoked.text
    assert client.get("/api/v1/auth/me", headers=user_auth).status_code == 401
    with SessionLocal() as db:
        assert db.scalar(select(AccessAudit).where(
            AccessAudit.path == "/api/v1/admin/users",
            AccessAudit.status_code == 201
        )) is not None


def test_rotation_scope_updates_and_disabled_access(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    catalog()
    auth, _, _ = admin_identity()
    create = client.post("/api/v1/admin/users", headers=auth, json={
        "name": "analyst_one", "role": "analyst",
    })
    assert create.status_code == 201
    p = create.json()
    old = {"Authorization": "Bearer " + p["tokenOnce"]}
    changed = client.patch("/api/v1/admin/users/" + p["id"], headers=auth,
                           json={"stores": ["S001"], "categories": ["Food"]})
    assert changed.status_code == 200
    assert client.get("/api/v1/auth/me", headers=old).json()["stores"] == ["S001"]
    rotate = client.post(f"/api/v1/admin/keys/{p['key']['id']}/rotate",
                         headers=auth, json={"label": "rotation", "expiresInDays": 10})
    assert rotate.status_code == 200, rotate.text
    secret = rotate.json()["tokenOnce"]
    assert client.get("/api/v1/auth/me", headers=old).status_code == 401
    fresh = {"Authorization": "Bearer " + secret}
    assert client.get("/api/v1/auth/me", headers=fresh).status_code == 200
    disabled = client.patch("/api/v1/admin/users/" + p["id"], headers=auth,
                            json={"enabled": False})
    assert disabled.status_code == 200
    assert client.get("/api/v1/auth/me", headers=fresh).status_code == 401


def test_admin_self_lockout_blocked(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    auth, user_id, key_id = admin_identity()
    assert client.patch(f"/api/v1/admin/users/{user_id}",
                        headers=auth, json={"enabled": False}).status_code == 409
    assert client.patch(f"/api/v1/admin/users/{user_id}",
                        headers=auth, json={"role": "analyst"}).status_code == 409
    assert client.post(f"/api/v1/admin/keys/{key_id}/revoke",
                       headers=auth, json={}).status_code == 409
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 200


def test_scope_validation_and_expiry(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    catalog()
    auth, _, _ = admin_identity()
    assert client.post("/api/v1/admin/users", headers=auth,
                       json={"name": "category_bad", "role": "category_manager"}
                       ).status_code == 400
    assert client.post("/api/v1/admin/users", headers=auth,
                       json={"name": "observer_bad", "role": "observer",
                             "stores": ["S404"]}).status_code == 400
    created = client.post("/api/v1/admin/users", headers=auth, json={
        "name": "category_good", "role": "category_manager",
        "categories": ["Food"]
    }).json()
    credential = {"Authorization": "Bearer " + created["tokenOnce"]}
    assert client.get("/api/v1/auth/me", headers=credential).status_code == 200
    with SessionLocal.begin() as db:
        db.get(AccessKey, created["key"]["id"]).expires_at = utc_now() - timedelta(days=1)
    assert client.get("/api/v1/auth/me", headers=credential).status_code == 401


def test_database_rate_limiting_and_request_size(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "3")
    headers = {"Authorization": "Bearer demo-analyst"}
    results = [client.get("/api/v1/auth/me", headers=headers) for _ in range(4)]
    assert [r.status_code for r in results] == [200, 200, 200, 429]
    assert results[-1].headers["retry-after"] == "60"
    monkeypatch.setenv("MAX_REQUEST_BYTES", "100")
    response = client.post("/api/v1/forecasts/backtest", headers=headers,
                           json={"large": "x" * 300})
    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"


def test_audit_contains_no_credentials(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    auth, _, _ = admin_identity()
    response = client.post("/api/v1/admin/users", headers=auth, json={
        "name": "audited_identity", "role": "analyst",
    })
    assert response.status_code == 201
    key = response.json()["tokenOnce"]
    logs = client.get("/api/v1/admin/audit?limit=100", headers=auth)
    assert logs.status_code == 200
    assert any(x["method"] == "POST" and x["status"] == 201 for x in logs.json())
    assert key not in logs.text
    with SessionLocal() as db:
        assert all(key not in x.path for x in db.scalars(select(AccessAudit)))


def test_offline_first_admin_bootstrap_only_once(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "0")
    from backend.scripts.manage_access import bootstrap
    secret, key_id = bootstrap("first")
    assert secret.startswith("da_")
    assert client.get("/api/v1/auth/me", headers={
        "Authorization": "Bearer " + secret
    }).json()["role"] == "access_admin"
    with pytest.raises(RuntimeError, match="already exists"):
        bootstrap("second")
    # An administrative recovery is possible only after ALL admin keys expire.
    from backend.scripts.manage_access import recover
    with pytest.raises(RuntimeError, match="active administrator key"):
        recover("first")
    with SessionLocal.begin() as db:
        db.get(AccessKey, key_id).expires_at = utc_now() - timedelta(days=1)
    new_secret, _ = recover("first")
    assert client.get("/api/v1/auth/me", headers={
        "Authorization": "Bearer " + secret
    }).status_code == 401
    assert client.get("/api/v1/auth/me", headers={
        "Authorization": "Bearer " + new_secret
    }).status_code == 200
    with SessionLocal() as db:
        assert db.scalar(select(AccessAudit).where(
            AccessAudit.path == "/admin/recover"
        )) is not None

