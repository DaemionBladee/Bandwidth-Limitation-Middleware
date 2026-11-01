import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bandwidth.accounting import JSONLAccounting, ingest_batch, ingest_record, radius_measurement
from bandwidth.api import app
from bandwidth.db import transaction
from bandwidth.engine import evaluate
from bandwidth.models import AccountingRecord, Assignment, Audit, Checkpoint, PolicyVersion, User
from tests.helpers import measurement, setup_user, spec

AUTH = {"Authorization": "Bearer test-automation-token"}


def set_default(client, **overrides):
    result = client.post(
        "/api/default-plan", json={"spec": spec(**overrides).model_dump(mode="json")}, headers=AUTH
    )
    assert result.status_code == 201, result.text
    return result.json()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_default_requires_admin(method):
    with TestClient(app) as client:
        assert client.request(method, "/api/default-plan").status_code == 401


def test_default_crud_and_individual_allowances(clock):
    with TestClient(app) as client:
        assert not client.get("/api/default-plan", headers=AUTH).json()["configured"]
        plan = set_default(client)
        assert client.get("/api/dashboard", headers=AUTH).json()["default_plan"] == plan
        assert (
            client.post("/api/default-plan", json={"policy_id": plan["policy_id"]}, headers=AUTH).status_code
            == 409
        )
        for name in ("alice", "bob"):
            response = client.post("/api/users", json={"username": name}, headers=AUTH)
            assert response.status_code == 201
            assert response.json()["default_assigned"]
        with transaction() as db:
            ingest_record(db, "test", measurement(clock, upload=100))
        users = client.get("/api/users", headers=AUTH).json()
        assert [(u["used_bytes"], u["quota_bytes"]) for u in users] == [(100, 100), (0, 100)]
        edited = client.put(
            "/api/default-plan",
            json={
                "spec": spec(quota_bytes=250).model_dump(mode="json"),
                "expected_revision": plan["revision"],
            },
            headers=AUTH,
        )
        assert edited.status_code == 200
        latest = edited.json()
        assert latest["spec"]["quota_bytes"] == 250
        client.post("/api/users", json={"username": "charlie"}, headers=AUTH)
        users = client.get("/api/users", headers=AUTH).json()
        assert [u["quota_bytes"] for u in users] == [100, 100, 250]
        assert users[0]["used_bytes"] == 100 and users[0]["ise"]["desired"]["mode"] == "LIMITED"
        assert (
            client.delete(f"/api/default-plan?expected_revision={plan['revision']}", headers=AUTH).status_code
            == 409
        )
        assert (
            client.delete(
                f"/api/default-plan?expected_revision={latest['revision']}", headers=AUTH
            ).status_code
            == 200
        )
        assert (
            client.post("/api/users", json={"username": "unassigned"}, headers=AUTH).json()[
                "default_assigned"
            ]
            is False
        )
        assert len(client.get("/api/policies", headers=AUTH).json()) == 1
        actions = client.get("/api/audit", headers=AUTH).json()
        assert {"default_plan.create", "default_plan.edit", "default_plan.delete", "assignment.default"} <= {
            a["action"] for a in actions
        }
        assert next(a for a in actions if a["action"] == "assignment.default")["entity_label"] == "charlie"


def test_radius_feed_discovers_and_reconnect_does_not_reset(clock, tmp_path):
    with TestClient(app) as client:
        plan = set_default(client)
    at = clock.now - timedelta(minutes=2)
    raw = {
        "record_id": "nas-a:42:1",
        "User-Name": "new@example.com",
        "Acct-Unique-Session-Id": "nas-a:42",
        "Acct-Session-Id": "42",
        "Event-Timestamp": at.timestamp(),
        "Acct-Input-Octets": 40,
        "Acct-Output-Octets": 60,
    }
    path = tmp_path / "radius.jsonl"
    path.write_text(json.dumps(raw) + "\n")
    with transaction() as db:
        assert ingest_batch(db, JSONLAccounting(path, radius=True)) == 1
        user = db.scalar(select(User).where(User.username == "new@example.com"))
        uid = user.id
        assignment = db.scalar(select(Assignment).where(Assignment.user_id == uid))
        assert assignment.effective_from == at
        assert db.get(PolicyVersion, assignment.version_id).policy_id == plan["policy_id"]
        assert evaluate(db, uid)[1].used_bytes == 100
        period_id = evaluate(db, uid)[1].id
    with path.open("a") as output:
        output.write(json.dumps(raw) + "\n")
        output.write(
            json.dumps(
                {
                    **raw,
                    "record_id": "nas-a:43:1",
                    "Acct-Unique-Session-Id": "nas-a:43",
                    "Event-Timestamp": clock.now.timestamp(),
                    "Acct-Input-Octets": 5,
                    "Acct-Output-Octets": 5,
                }
            )
            + "\n"
        )
    with transaction() as db:
        assert ingest_batch(db, JSONLAccounting(path, radius=True)) == 1
        assert evaluate(db, uid)[1].id == period_id
        assert evaluate(db, uid)[1].used_bytes == 110
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.scalar(select(func.count()).select_from(Assignment)) == 1
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "user.discover")) == 1


def test_concurrent_first_records_create_one_subscriber_and_assignment(clock):
    with TestClient(app) as client:
        set_default(client, quota_bytes=1000)

    def send(i):
        with transaction() as db:
            return ingest_record(
                db, "feed", measurement(clock, f"r{i % 12}", upload=5, download=5, username="new")
            )

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(send, range(24))) == 12
    with transaction() as db:
        user = db.scalar(select(User))
        assert evaluate(db, user.id)[1].used_bytes == 120
        assert db.scalar(select(func.count()).select_from(User)) == 1
        assert db.scalar(select(func.count()).select_from(Assignment)) == 1
        assert (
            db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "assignment.default"))
            == 1
        )


def test_discovery_rolls_back_with_invalid_batch_and_disabled_default(clock, tmp_path):
    with TestClient(app) as client:
        plan = set_default(client)
        payload = spec(enabled=False).model_dump(mode="json")
        assert (
            client.put(
                "/api/default-plan",
                json={"spec": payload, "expected_revision": plan["revision"]},
                headers=AUTH,
            ).status_code
            == 200
        )
    with pytest.raises(ValueError, match="not registered"):
        with transaction() as db:
            ingest_record(db, "feed", measurement(clock, username="new"))
    with TestClient(app) as client:
        current = client.get("/api/default-plan", headers=AUTH).json()
        client.put(
            "/api/default-plan",
            json={"spec": spec().model_dump(mode="json"), "expected_revision": current["revision"]},
            headers=AUTH,
        )
    path = tmp_path / "records.jsonl"
    records = [
        measurement(clock, "good", username="new"),
        measurement(clock, "future", username="new2", observed_at=clock.now + timedelta(days=1)),
    ]
    path.write_text("".join(json.dumps(r.model_dump(mode="json")) + "\n" for r in records))
    with pytest.raises(ValueError, match="Future"):
        with transaction() as db:
            ingest_batch(db, JSONLAccounting(path))
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 0
        assert db.get(Checkpoint, "jsonl") is None


def test_existing_assignments_and_unassigned_users_are_preserved(clock):
    uid, pid, _ = setup_user(clock)
    with transaction() as db:
        db.add(User(username="unassigned"))
    with TestClient(app) as client:
        plan = client.post("/api/default-plan", json={"policy_id": pid}, headers=AUTH).json()
        body = {"spec": spec(quota_bytes=500).model_dump(mode="json"), "expected_revision": plan["revision"]}
        assert client.put("/api/default-plan", json=body, headers=AUTH).status_code == 200
        assert client.put("/api/default-plan", json=body, headers=AUTH).status_code == 409
    with transaction() as db:
        ingest_record(db, "feed", measurement(clock, upload=50))
        ingest_record(db, "feed", measurement(clock, "unassigned", username="unassigned", upload=10))
        assert evaluate(db, uid)[1].limit_bytes == 100
        other = db.scalar(select(User).where(User.username == "unassigned"))
        assert evaluate(db, other.id) == (None, None, None)


def test_concurrent_default_updates_and_stale_remove(clock):
    with TestClient(app) as client:
        plan = set_default(client)

    def update(quota):
        with TestClient(app) as client:
            return client.put(
                "/api/default-plan",
                json={
                    "spec": spec(quota_bytes=quota).model_dump(mode="json"),
                    "expected_revision": plan["revision"],
                },
                headers=AUTH,
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, [200, 300])) == [200, 409]
    with TestClient(app) as client:
        current = client.get("/api/default-plan", headers=AUTH).json()
        assert (
            client.delete(
                f"/api/default-plan?expected_revision={current['revision']}", headers=AUTH
            ).status_code
            == 200
        )
        restored = client.post(
            "/api/default-plan", json={"policy_id": plan["policy_id"]}, headers=AUTH
        ).json()
        assert restored["revision"] > current["revision"]
        assert (
            client.delete(
                f"/api/default-plan?expected_revision={current['revision']}", headers=AUTH
            ).status_code
            == 409
        )


def test_invalid_default_and_mock_discovery_api(clock):
    with TestClient(app) as client:
        assert client.post("/api/default-plan", json={}, headers=AUTH).status_code == 422
        assert client.post("/api/default-plan", json={"policy_id": 999}, headers=AUTH).status_code == 404
        assert (
            client.post(
                "/api/default-plan",
                json={"spec": spec(quota_bytes=1).model_dump(mode="json"), "policy_id": 1},
                headers=AUTH,
            ).status_code
            == 422
        )
        set_default(client)
        payload = measurement(clock, username="from-wlc").model_dump(mode="json")
        assert client.post("/api/mock/accounting", json=payload).status_code == 401
        assert client.post("/api/mock/accounting", json=payload, headers=AUTH).status_code == 202
    with transaction() as db:
        assert ingest_batch(db) == 1
        assert db.scalar(select(User).where(User.username == "from-wlc")) is not None


def test_radius_start_without_counters_discovers_user_immediately(clock):
    with TestClient(app) as client:
        set_default(client)
    raw = {
        "record_id": "start-1",
        "User-Name": "new-wlc-user",
        "Acct-Session-Id": "nas-a:s1",
        "Event-Timestamp": clock.now.timestamp(),
        "Acct-Status-Type": "Start",
    }
    record = radius_measurement(raw)
    assert record.counters() == (0, 0)
    with transaction() as db:
        ingest_record(db, "radius-jsonl", record)
        user = db.scalar(select(User).where(User.username == "new-wlc-user"))
        _, period, _ = evaluate(db, user.id)
        assert period.used_bytes == 0 and period.limit_bytes == 100
    with pytest.raises(ValueError, match="missing Acct-Input-Octets"):
        radius_measurement({**raw, "Acct-Status-Type": "Interim-Update"})


def test_ordinary_policy_edit_updates_default_revision(clock):
    with TestClient(app) as client:
        plan = set_default(client)
        body = {
            "spec": spec(quota_bytes=200).model_dump(mode="json"),
            "expected_revision": 1,
            "application": "next_period",
        }
        assert client.put(f"/api/policies/{plan['policy_id']}", json=body, headers=AUTH).status_code == 200
        current = client.get("/api/default-plan", headers=AUTH).json()
        assert current["revision"] == plan["revision"] + 1
        assert current["spec"]["quota_bytes"] == 200
        assert (
            client.delete(f"/api/default-plan?expected_revision={plan['revision']}", headers=AUTH).status_code
            == 409
        )
