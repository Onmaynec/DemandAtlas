"""PostgreSQL Compose integration for managed identity credentials."""
import os
from fastapi.testclient import TestClient

# Exercise DB-backed auth, not the locally configured demo profiles.
os.environ["DEMO_MODE"] = "0"

from backend.app.main import app  # noqa: E402
from backend.scripts.manage_access import bootstrap  # noqa: E402

client = TestClient(app)


def run():
    secret, _ = bootstrap("smoke_admin")
    auth = {"Authorization": "Bearer " + secret}
    assert client.get("/api/v1/auth/me", headers=auth).json()["role"] == "access_admin"
    assert client.get("/api/v1/catalog", headers=auth).status_code == 403
    assert client.get("/api/v1/auth/me", headers={
        "Authorization": "Bearer demo-analyst"
    }).status_code == 401
    scoped = client.post("/api/v1/admin/users", headers=auth, json={
        "name": "smoke_observer", "role": "observer", "stores": ["S001"],
        "categories": ["Food"]
    })
    assert scoped.status_code == 201, scoped.text
    token = scoped.json()["tokenOnce"]
    user_auth = {"Authorization": "Bearer " + token}
    assert client.get("/api/v1/catalog", headers=user_auth).status_code == 200
    assert client.get("/api/v1/metrics/sales?start=2026-01-01&end=2026-01-02&store=S002",
                      headers=user_auth).status_code == 403
    assert client.post(
        "/api/v1/admin/keys/" + scoped.json()["key"]["id"] + "/revoke",
        headers=auth, json={}
    ).status_code == 200
    assert client.get("/api/v1/auth/me", headers=user_auth).status_code == 401
    assert client.get("/api/v1/admin/audit", headers=auth).json()
    print("Managed identity and revocation smoke passed on PostgreSQL")


if __name__ == "__main__":
    run()
