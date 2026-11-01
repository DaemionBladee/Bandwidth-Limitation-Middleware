import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bandwidth.api import app
from bandwidth.auth import password_hash
from bandwidth.db import transaction
from bandwidth.models import Admin, AdminSession
from bandwidth.permissions import ALL_PERMISSIONS
from tests.helpers import spec

OWNER_PASSWORD = "owner-unique-passphrase"
USER_PASSWORD = "individual-user-passphrase"


def login(client, username, password):
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf"]}


def owner(client):
    with transaction() as db:
        db.add(Admin(username="owner", role="admin", password_hash=password_hash(OWNER_PASSWORD)))
    return login(client, "owner", OWNER_PASSWORD)


def create(client, headers, name="reader", **changes):
    body = {
        "username": name,
        "password": USER_PASSWORD,
        "confirm_password": USER_PASSWORD,
        "current_password": OWNER_PASSWORD,
        **changes,
    }
    response = client.post("/api/accounts", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def edit_body(account, **changes):
    return {
        "username": account["username"],
        "role": account["role"],
        "permissions": account["permissions"],
        "enabled": account["enabled"],
        "expected_revision": account["revision"],
        "current_password": OWNER_PASSWORD,
        **changes,
    }


def test_default_user_read_only_and_filtered_snapshot(clock):
    with TestClient(app) as admin, TestClient(app) as user:
        headers = owner(admin)
        account = create(admin, headers)
        assert account["role"] == "user" and account["permissions"] == ["read"]
        user_headers = login(user, "reader", USER_PASSWORD)
        session = user.get("/api/session").json()
        assert session["role"] == "user" and session["permissions"] == ["read"]
        for path in (
            "/api/policies",
            "/api/users",
            "/api/status",
            "/api/audit",
            "/api/timezones",
            "/api/default-plan",
        ):
            assert user.get(path).status_code == 200
        snapshot = user.get("/api/dashboard").json()
        assert snapshot["admins"] == [] and snapshot["access"]["role"] == "user"
        assert snapshot["audit"] == []
        assert user.get("/api/audit").json() == []
        assert user.get("/api/accounts").status_code == 403
        assert user.get("/api/admins").status_code == 403
        assert user.post("/api/logout", headers=user_headers).status_code == 200
        assert user.get("/api/dashboard").status_code == 401


@pytest.mark.parametrize(
    "method,path,permission",
    [
        ("POST", "/api/policies", "policies.create"),
        ("PUT", "/api/policies/1", "policies.update"),
        ("POST", "/api/default-plan", "default_plan.create"),
        ("PUT", "/api/default-plan", "default_plan.update"),
        ("DELETE", "/api/default-plan?expected_revision=1", "default_plan.delete"),
        ("POST", "/api/users", "subscribers.create"),
        ("PUT", "/api/users/1/assignment", "assignments.update"),
        ("POST", "/api/users/1/reset-usage", "usage.reset"),
        ("POST", "/api/users/1/reconcile", "enforcement.reconcile"),
        ("POST", "/api/mock/accounting", "accounting.submit"),
    ],
)
def test_read_only_denies_every_business_mutation(method, path, permission):
    with TestClient(app) as admin, TestClient(app) as user:
        headers = owner(admin)
        create(admin, headers)
        user_headers = login(user, "reader", USER_PASSWORD)
        response = user.request(method, path, json={}, headers=user_headers)
        assert response.status_code == 403
        assert response.json()["detail"] == f"Permission required: {permission}"


def test_specific_grant_allows_only_assigned_action():
    with TestClient(app) as admin, TestClient(app) as user:
        headers = owner(admin)
        create(admin, headers, permissions=["subscribers.create"])
        user_headers = login(user, "reader", USER_PASSWORD)
        assert (
            user.post("/api/users", json={"username": "subscriber"}, headers=user_headers).status_code == 201
        )
        assert (
            user.post("/api/policies", json=spec().model_dump(mode="json"), headers=user_headers).status_code
            == 403
        )
        assert user.get("/api/accounts").status_code == 403
        assert user.post("/api/users", json={"username": "no-csrf"}).status_code == 403
        assert user.get("/api/users").json()[0]["username"] == "subscriber"


def test_all_business_grants_cannot_manage_accounts_or_escalate():
    with TestClient(app) as admin, TestClient(app) as user:
        headers = owner(admin)
        account = create(admin, headers, permissions=ALL_PERMISSIONS)
        user_headers = login(user, "reader", USER_PASSWORD)
        for method, path in (
            ("POST", "/api/accounts"),
            ("PUT", f"/api/accounts/{account['id']}"),
            ("DELETE", f"/api/accounts/{account['id']}"),
            ("POST", "/api/admins"),
        ):
            assert (
                user.request(
                    method,
                    path,
                    json={"role": "admin", "current_password": USER_PASSWORD},
                    headers=user_headers,
                ).status_code
                == 403
            )
        invalid = {
            "username": "invalid",
            "password": USER_PASSWORD,
            "confirm_password": USER_PASSWORD,
            "current_password": OWNER_PASSWORD,
            "permissions": ["accounts.create"],
        }
        assert admin.post("/api/accounts", json=invalid, headers=headers).status_code == 422
        assert "password" not in admin.get("/api/accounts").text


def test_account_crud_revokes_sessions_and_preserves_audit():
    with TestClient(app) as admin, TestClient(app) as user:
        headers = owner(admin)
        account = create(admin, headers)
        login(user, "reader", USER_PASSWORD)
        body = edit_body(account, permissions=["read", "policies.create"])
        response = admin.put(f"/api/accounts/{account['id']}", json=body, headers=headers)
        assert response.status_code == 200
        changed = response.json()
        assert changed["revision"] == account["revision"] + 1
        assert user.get("/api/session").status_code == 401
        assert admin.put(f"/api/accounts/{account['id']}", json=body, headers=headers).status_code == 409
        user_headers = login(user, "reader", USER_PASSWORD)
        assert (
            user.post("/api/policies", json=spec().model_dump(mode="json"), headers=user_headers).status_code
            == 201
        )
        disabled = admin.put(
            f"/api/accounts/{account['id']}", json=edit_body(changed, enabled=False), headers=headers
        ).json()
        assert disabled["enabled"] is False
        assert user.get("/api/session").status_code == 401
        assert (
            user.post("/api/login", json={"username": "reader", "password": USER_PASSWORD}).status_code == 401
        )
        deletion = {"expected_revision": disabled["revision"], "current_password": OWNER_PASSWORD}
        assert (
            admin.request(
                "DELETE", f"/api/accounts/{account['id']}", json=deletion, headers=headers
            ).status_code
            == 200
        )
        assert admin.get(f"/api/accounts/{account['id']}").status_code == 404
        audits = admin.get("/api/audit").json()
        assert {"account.create", "account.update", "account.delete"} <= {a["action"] for a in audits}
        serialized = json.dumps(audits)
        assert USER_PASSWORD not in serialized and OWNER_PASSWORD not in serialized
        with transaction() as db:
            assert db.scalar(select(func.count()).select_from(AdminSession)) == 1


def test_last_admin_protected_and_concurrent_account_revisions():
    with TestClient(app) as admin:
        headers = owner(admin)
        first = admin.get("/api/accounts").json()[0]
        assert (
            admin.put(
                f"/api/accounts/{first['id']}", json=edit_body(first, role="user"), headers=headers
            ).status_code
            == 409
        )
        assert (
            admin.put(
                f"/api/accounts/{first['id']}", json=edit_body(first, enabled=False), headers=headers
            ).status_code
            == 409
        )
        assert (
            admin.request(
                "DELETE",
                f"/api/accounts/{first['id']}",
                json={"expected_revision": 1, "current_password": OWNER_PASSWORD},
                headers=headers,
            ).status_code
            == 409
        )
        account = create(admin, headers)
        cookies = dict(admin.cookies)

        def update(enabled):
            with TestClient(app) as client:
                client.cookies.update(cookies)
                return client.put(
                    f"/api/accounts/{account['id']}",
                    json=edit_body(account, enabled=enabled),
                    headers=headers,
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(update, [True, False])) == [200, 409]


def test_password_changes_and_sensitive_action_checks():
    with TestClient(app) as admin, TestClient(app) as user:
        headers = owner(admin)
        account = create(admin, headers)
        login(user, "reader", USER_PASSWORD)
        changed_password = "a-replacement-user-passphrase"
        body = edit_body(account, password=changed_password, confirm_password=changed_password)
        assert admin.put(f"/api/accounts/{account['id']}", json=body).status_code == 403
        assert (
            admin.put(
                f"/api/accounts/{account['id']}", json={**body, "current_password": "wrong"}, headers=headers
            ).status_code
            == 403
        )
        assert (
            admin.put(
                f"/api/accounts/{account['id']}",
                json=body,
                headers={**headers, "Origin": "https://evil.test"},
            ).status_code
            == 403
        )
        assert admin.put(f"/api/accounts/{account['id']}", json=body, headers=headers).status_code == 200
        assert user.get("/api/session").status_code == 401
        assert (
            user.post("/api/login", json={"username": "reader", "password": USER_PASSWORD}).status_code == 401
        )
        assert (
            user.post("/api/login", json={"username": "reader", "password": changed_password}).status_code
            == 200
        )
        audits = admin.get("/api/audit").json()
        assert (
            next(a for a in audits if a["action"] == "account.update")["after"]["credentials_updated"] is True
        )
        assert changed_password not in json.dumps(audits)


def test_concurrent_admin_demotions_keep_one_enabled_admin():
    with TestClient(app) as first, TestClient(app) as second:
        first_headers = owner(first)
        second_account = create(first, first_headers, name="second", role="admin")
        second_headers = login(second, "second", USER_PASSWORD)
        first_account = first.get("/api/accounts").json()[0]
        inputs = [
            (dict(first.cookies), first_headers, first_account, OWNER_PASSWORD),
            (dict(second.cookies), second_headers, second_account, USER_PASSWORD),
        ]

        def demote(item):
            cookies, headers, account, password = item
            with TestClient(app) as client:
                client.cookies.update(cookies)
                return client.put(
                    f"/api/accounts/{account['id']}",
                    json=edit_body(account, role="user", current_password=password),
                    headers=headers,
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(demote, inputs)) == [200, 409]
        with transaction() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(Admin)
                    .where(Admin.role == "admin", Admin.enabled.is_(True))
                )
                == 1
            )
