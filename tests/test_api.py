import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bandwidth.api import app
from bandwidth.auth import password_hash
from bandwidth.db import transaction
from bandwidth.models import Admin, Audit, PolicyVersion
from tests.helpers import setup_user, spec


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def bearer():
    return {"Authorization": "Bearer test-automation-token"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/api/policies"),
        ("POST", "/api/policies"),
        ("GET", "/api/users"),
        ("POST", "/api/users"),
        ("PUT", "/api/users/1/assignment"),
        ("GET", "/api/audit"),
        ("GET", "/api/status"),
        ("POST", "/api/mock/accounting"),
        ("GET", "/api/users/1/history"),
        ("POST", "/api/users/1/reconcile"),
    ],
)
def test_admin_authorization_required(client, method, path):
    assert client.request(method, path).status_code == 401


def test_admin_cookie_csrf_and_logout(client):
    with transaction() as db:
        db.add(Admin(role="admin", username="admin", password_hash=password_hash("long-test-password")))
    assert client.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    response = client.post("/api/login", json={"username": "admin", "password": "long-test-password"})
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.post("/api/users", json={"username": "alice"}).status_code == 403
    headers = {"X-CSRF-Token": response.json()["csrf"]}
    assert client.post("/api/users", json={"username": "alice"}, headers=headers).status_code == 201
    assert (
        client.post(
            "/api/users", json={"username": "bob"}, headers={**headers, "Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/users").status_code == 401


def test_policy_edits_audit_and_optimistic_revision(client, clock):
    _, pid, _ = setup_user(clock)
    body = {
        "spec": spec(quota_bytes=250).model_dump(mode="json"),
        "application": "next_period",
        "expected_revision": 1,
    }
    assert client.put(f"/api/policies/{pid}", json=body, headers=bearer()).status_code == 200
    assert client.put(f"/api/policies/{pid}", json=body, headers=bearer()).status_code == 409
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(PolicyVersion)) == 2
        audit = db.scalar(select(Audit).where(Audit.action == "policy.edit"))
        assert audit.before["quota_bytes"] == 100
        assert audit.after["spec"]["quota_bytes"] == 250
    user = client.get("/api/users", headers=bearer()).json()[0]
    assert user["quota_bytes"] == 100
    assert user["pending"]


def test_validation_and_name_conflicts(client):
    headers = bearer()
    payload = spec().model_dump(mode="json")
    assert (
        client.post("/api/policies", json={**payload, "quota_bytes": 0}, headers=headers).status_code == 422
    )
    assert client.post("/api/policies", json=payload, headers=headers).status_code == 201
    assert client.post("/api/policies", json=payload, headers=headers).status_code == 409


def test_dashboard_and_health(client):
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/")
    assert response.status_code == 200
    assert "Bandwidth Engine" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200
