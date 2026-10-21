import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bandwidth.api import app
from bandwidth.auth import password_hash, password_matches
from bandwidth.cli import bootstrap
from bandwidth.db import transaction
from bandwidth.models import Admin, Audit

PASSWORD = "a-long-existing-password"
NEW_PASSWORD = "a-new-individual-passphrase"


def sign_in(client):
    with transaction() as db:
        db.add(Admin(username="owner", password_hash=password_hash(PASSWORD)))
    response = client.post("/api/login", json={"username": "owner", "password": PASSWORD})
    return {"X-CSRF-Token": response.json()["csrf"]}


def payload(**changes):
    return {
        "username": "operator",
        "password": NEW_PASSWORD,
        "confirm_password": NEW_PASSWORD,
        "current_password": PASSWORD,
        **changes,
    }


def test_secure_admin_creation_and_sign_in():
    with TestClient(app) as client:
        assert client.get("/api/admins").status_code == 401
        assert client.post("/api/admins", json=payload()).status_code == 401
        assert (
            client.post(
                "/api/admins", json=payload(), headers={"Authorization": "Bearer test-automation-token"}
            ).status_code
            == 403
        )
        headers = sign_in(client)
        assert client.post("/api/admins", json=payload()).status_code == 403
        assert (
            client.post(
                "/api/admins", json=payload(), headers={**headers, "Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post("/api/admins", json=payload(current_password="wrong"), headers=headers).status_code
            == 403
        )
        response = client.post("/api/admins", json=payload(), headers=headers)
        assert response.status_code == 201
        assert set(response.json()) == {"id", "username"}
        assert client.post("/api/admins", json=payload(), headers=headers).status_code == 409
        listing = client.get("/api/admins").json()
        assert [a["username"] for a in listing] == ["owner", "operator"]
        assert all(set(a) == {"id", "username"} for a in listing)
        audit = client.get("/api/audit").json()
        assert len([a for a in audit if a["action"] == "admin.create"]) == 1
        serialized = json.dumps(audit)
        assert NEW_PASSWORD not in serialized and PASSWORD not in serialized and "password" not in serialized
        with transaction() as db:
            account = db.scalar(select(Admin).where(Admin.username == "operator"))
            assert account.password_hash.startswith("scrypt$")
            assert account.password_hash != NEW_PASSWORD and password_matches(
                NEW_PASSWORD, account.password_hash
            )
        client.post("/api/logout", headers=headers)
        assert (
            client.post("/api/login", json={"username": "operator", "password": NEW_PASSWORD}).status_code
            == 200
        )


def test_validation_does_not_echo_secrets():
    with TestClient(app) as client:
        headers = sign_in(client)
        for values in (
            payload(password="tiny-secret", confirm_password="tiny-secret"),
            payload(confirm_password="mismatched-secret"),
            payload(username="contains space"),
        ):
            response = client.post("/api/admins", json=values, headers=headers)
            assert response.status_code == 422
            assert "input" not in response.text
            for secret in (values["password"], values["confirm_password"], values["current_password"]):
                assert secret not in response.text
        with transaction() as db:
            assert db.scalar(select(func.count()).select_from(Admin)) == 1
            assert db.scalar(select(func.count()).select_from(Audit)) == 0


def test_password_confirmation_is_rate_limited(clock):
    with TestClient(app) as client:
        headers = sign_in(client)
        for _ in range(5):
            assert (
                client.post(
                    "/api/admins", json=payload(current_password="wrong"), headers=headers
                ).status_code
                == 403
            )
        assert client.post("/api/admins", json=payload(), headers=headers).status_code == 429
        from datetime import timedelta

        clock.now += timedelta(minutes=6)
        assert client.post("/api/admins", json=payload(), headers=headers).status_code == 201


def test_bootstrap_confirms_password_without_overwriting_accounts(monkeypatch):
    monkeypatch.delenv("ADMIN_BOOTSTRAP_PASSWORD", raising=False)
    monkeypatch.setattr("bandwidth.cli.load_dotenv", lambda: None)
    answers = iter([NEW_PASSWORD, "mismatched"])
    monkeypatch.setattr("bandwidth.cli.getpass.getpass", lambda prompt: next(answers))
    with pytest.raises(SystemExit, match="Passwords do not match"):
        bootstrap("first-admin")
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(Admin)) == 0
    answers = iter([NEW_PASSWORD, NEW_PASSWORD])
    bootstrap("first-admin")
    # No more answers: calling bootstrap again must not prompt or overwrite.
    bootstrap("first-admin")
    with transaction() as db:
        account = db.scalar(select(Admin))
        assert account.username == "first-admin" and password_matches(NEW_PASSWORD, account.password_hash)
        assert db.scalar(select(func.count()).select_from(Audit)) == 1
