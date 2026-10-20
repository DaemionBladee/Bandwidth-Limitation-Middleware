from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from bandwidth.accounting import ingest_record
from bandwidth.db import transaction
from bandwidth.engine import assign, evaluate
from bandwidth.models import EnforcementState, Job, Period, PolicyVersion
from tests.helpers import measurement, setup_user, spec


def new_version(db, policy_id, revision, **overrides):
    version = PolicyVersion(
        policy_id=policy_id, revision=revision, spec=spec(**overrides).model_dump(mode="json")
    )
    db.add(version)
    db.flush()
    return version


def test_immediate_edit_carries_usage(clock):
    uid, pid, _ = setup_user(clock)
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, upload=60))
        previous_period = evaluate(db, uid)[1].id
        version = new_version(db, pid, 2, quota_bytes=50)
        assign(db, uid, version.id, True, "immediate")
        period = evaluate(db, uid)[1]
        assert period.used_bytes == 60
        assert period.id != previous_period
        assert db.get(Period, previous_period).used_bytes == 60
        assert db.get(EnforcementState, uid).desired["mode"] == "LIMITED"


def test_next_period_edit_and_changed_schedule_fresh_allowance(clock):
    uid, pid, _ = setup_user(clock)
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, upload=100))
        version = new_version(db, pid, 2, period_type="monthly", quota_bytes=50)
        row = assign(db, uid, version.id, True, "next_period")
        boundary = row.effective_from
        assert evaluate(db, uid)[1].limit_bytes == 100
    clock.now = boundary
    with transaction() as db:
        _, current, _ = evaluate(db, uid)
        assert current.starts_at == boundary
        assert current.used_bytes == 0
        assert current.limit_bytes == 50
        assert db.get(EnforcementState, uid).desired["mode"] == "NORMAL"


def test_rollover_is_concurrent_and_retains_completed_period(clock):
    uid, _, _ = setup_user(clock)
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, upload=101))
        previous = evaluate(db, uid)[1]
        old_id, boundary = previous.id, previous.ends_at
    clock.now = boundary

    def roll(_):
        with transaction() as db:
            return evaluate(db, uid)[1].id

    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len(set(pool.map(roll, range(10)))) == 1
    with transaction() as db:
        assert db.get(Period, old_id).used_bytes == 101
        state = db.get(EnforcementState, uid)
        assert state.desired["mode"] == "NORMAL"
        assert (
            db.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.dedupe_key == f"enforce:{uid}:{state.generation}")
            )
            == 1
        )


def test_one_time_expiry_is_explicit(clock):
    policy = spec(
        period_type="custom",
        custom_start=clock.now - timedelta(hours=1),
        custom_end=clock.now + timedelta(hours=1),
        expiry_action="LIMITED",
    )
    uid, _, _ = setup_user(clock, policy)
    clock.now += timedelta(hours=1)
    with transaction() as db:
        assert evaluate(db, uid)[1] is None
        assert db.get(EnforcementState, uid).desired["mode"] == "LIMITED"


def test_outbox_rolls_back_with_decision(clock):
    uid, _, _ = setup_user(clock)
    with transaction() as db:
        original = db.get(EnforcementState, uid).generation
    with pytest.raises(RuntimeError):
        with transaction() as db:
            ingest_record(db, "test", measurement(clock, upload=200))
            raise RuntimeError("Simulated crash before commit")
    with transaction() as db:
        assert db.get(EnforcementState, uid).generation == original
        assert db.scalar(select(func.count()).select_from(Job)) == 1
