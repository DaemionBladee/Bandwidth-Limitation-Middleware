from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bandwidth.accounting import ingest_record
from bandwidth.api import app
from bandwidth.db import transaction
from bandwidth.engine import UsageResetConflict, assign, evaluate, reset_usage
from bandwidth.ise import MockISE
from bandwidth.models import (
    AccountingRecord,
    Assignment,
    Audit,
    EnforcementAttempt,
    EnforcementState,
    Period,
    PolicyVersion,
    UsageReset,
)
from bandwidth.schemas import ResetUsageInput
from bandwidth.worker import run_once
from tests.helpers import measurement, setup_user, spec

AUTH = {"Authorization": "Bearer test-automation-token"}


def prepare(clock, amount=100, cumulative=False):
    uid, pid, _ = setup_user(clock)
    with transaction() as db:
        ingest_record(
            db,
            "test",
            measurement(clock, "initial", upload=amount, kind="cumulative" if cumulative else "delta"),
        )
        period = evaluate(db, uid)[1]
        return uid, pid, period.id, period.ends_at


def request(period_id, **overrides):
    return ResetUsageInput(
        expected_period_id=period_id, request_id=uuid4(), reason="Support approved", **overrides
    )


@pytest.mark.parametrize("amount", [99, 100, 101])
def test_reset_restores_fresh_allowance_preserves_usage_and_queues_normal(clock, amount):
    uid, _, old_id, next_reset = prepare(clock, amount)
    with transaction() as db:
        row = reset_usage(db, uid, request(old_id), "admin")
        current = evaluate(db, uid)[1]
        assert row.new_period_id == current.id != old_id
        assert current.starts_at == clock.now and current.ends_at == next_reset
        assert current.used_bytes == 0
        assert db.get(Period, old_id).used_bytes == amount
        assert db.get(Period, old_id).ends_at == clock.now
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 1
        state = db.get(EnforcementState, uid)
        assert state.desired["mode"] == "NORMAL" and state.status == "pending"
        audit = db.scalar(select(Audit).where(Audit.action == "usage.reset"))
        assert audit.before["used_bytes"] == amount
        assert audit.after["used_bytes"] == 0
        assert audit.after["reason"] == "Support approved"


def test_concurrent_replay_is_idempotent(clock):
    uid, _, old_id, _ = prepare(clock)
    body = request(old_id)

    def reset(_):
        with transaction() as db:
            return reset_usage(db, uid, body, "admin").id

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len(set(pool.map(reset, range(10)))) == 1
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(UsageReset)) == 1
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "usage.reset")) == 1


def test_concurrent_conflicting_resets_require_new_confirmation(clock):
    uid, _, old_id, _ = prepare(clock)

    def reset(_):
        try:
            with transaction() as db:
                reset_usage(db, uid, request(old_id), "admin")
                return True
        except UsageResetConflict:
            return False

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert sum(pool.map(reset, range(5))) == 1


def test_counter_baseline_and_late_samples_span_reset_without_double_counting(clock):
    uid, _, old_id, next_reset = prepare(clock, 100, cumulative=True)
    with transaction() as db:
        reset_usage(db, uid, request(old_id), "admin")
    reset_at = clock.now
    clock.now += timedelta(minutes=2)
    with transaction() as db:
        ingest_record(
            db, "test", measurement(clock, "after", upload=130, kind="cumulative", observed_at=clock.now)
        )
        assert evaluate(db, uid)[1].used_bytes == 30
    with transaction() as db:
        # This delayed sample adjusts the old segment and the subsequent delta.
        ingest_record(
            db,
            "test",
            measurement(
                clock, "late", upload=110, kind="cumulative", observed_at=reset_at - timedelta(seconds=10)
            ),
        )
        current = evaluate(db, uid)[1]
        assert current.used_bytes == 20
        assert current.starts_at == reset_at
        assert db.get(Period, old_id).used_bytes == 110
        assert (
            len(
                db.scalars(
                    select(Period).where(
                        Period.user_id == uid,
                        Period.starts_at == reset_at.replace(hour=0, minute=0, second=0),
                    )
                ).all()
            )
            == 1
        )
    clock.now = next_reset
    with transaction() as db:
        fresh = evaluate(db, uid)[1]
        assert fresh.used_bytes == 0 and fresh.starts_at == next_reset
        assert db.get(Period, old_id).used_bytes == 110


def test_immediate_edit_respects_manual_reset(clock):
    uid, pid, old_id, _ = prepare(clock)
    with transaction() as db:
        reset_usage(db, uid, request(old_id), "admin")
    clock.now += timedelta(seconds=1)
    with transaction() as db:
        version = PolicyVersion(policy_id=pid, revision=2, spec=spec(quota_bytes=200).model_dump(mode="json"))
        db.add(version)
        db.flush()
        assign(db, uid, version.id, True, "immediate")
        assert evaluate(db, uid)[1].used_bytes == 0


def test_reset_api_authorization_validation_idempotency_and_history(clock):
    uid, _, old_id, _ = prepare(clock)
    body = request(old_id).model_dump(mode="json")
    with TestClient(app) as client:
        path = f"/api/users/{uid}/reset-usage"
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json={**body, "reason": "  "}, headers=AUTH).status_code == 422
        first = client.post(path, json=body, headers=AUTH)
        assert first.status_code == 200
        assert first.json()["user"]["used_bytes"] == 0
        again = client.post(path, json=body, headers=AUTH)
        assert again.json()["reset_id"] == first.json()["reset_id"]
        assert client.post(path, json={**body, "reason": "Different reason"}, headers=AUTH).status_code == 409
        assert client.post(path, json={**body, "request_id": str(uuid4())}, headers=AUTH).status_code == 409
        history = client.get(f"/api/users/{uid}/history", headers=AUTH).json()
        assert history["resets"][0]["reason"] == "Support approved"
        assert client.post("/api/users/9999/reset-usage", json=body, headers=AUTH).status_code == 404


def test_one_time_expiry_does_not_reactivate_from_reset(clock):
    uid, _, _ = setup_user(
        clock,
        spec(
            period_type="custom",
            custom_start=clock.now - timedelta(hours=2),
            custom_end=clock.now - timedelta(hours=1),
            expiry_action="LIMITED",
        ),
    )
    with pytest.raises(ValueError, match="active quota"):
        with transaction() as db:
            reset_usage(db, uid, request(1), "admin")


def test_pending_policy_change_remains_scheduled_after_reset(clock):
    uid, pid, old_id, next_reset = prepare(clock)
    with transaction() as db:
        version = PolicyVersion(policy_id=pid, revision=2, spec=spec(quota_bytes=250).model_dump(mode="json"))
        db.add(version)
        db.flush()
        pending = assign(db, uid, version.id, True, "next_period")
        pending_id = pending.id
        reset_usage(db, uid, request(old_id), "admin")
        assert not db.get(Assignment, pending_id).cancelled
        assert db.get(Assignment, pending_id).effective_from == next_reset
        assert evaluate(db, uid)[1].limit_bytes == 100
    clock.now = next_reset
    with transaction() as db:
        assert evaluate(db, uid)[1].limit_bytes == 250


def test_pre_reset_limited_job_never_overrides_new_allowance(clock):
    uid, _, old_id, _ = prepare(clock)
    with transaction() as db:
        reset_usage(db, uid, request(old_id), "admin")
    while run_once(MockISE()):
        pass
    with transaction() as db:
        state = db.get(EnforcementState, uid)
        assert state.confirmed["mode"] == "NORMAL"
        assert state.confirmed_generation == state.generation
        attempts = db.scalars(select(EnforcementAttempt).where(EnforcementAttempt.user_id == uid)).all()
        assert len(attempts) == 1 and attempts[0].desired["mode"] == "NORMAL"
