from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, select

from bandwidth.accounting import ingest_record
from bandwidth.config import settings
from bandwidth.db import transaction
from bandwidth.engine import evaluate
from bandwidth.ise import ISEError, MockISE
from bandwidth.models import EnforcementAttempt, EnforcementState, Job
from bandwidth.queue import claim, enqueue
from bandwidth.worker import process, reconcile_jobs, run_once
from tests.helpers import measurement, setup_user


class FlakyISE(MockISE):
    def __init__(self, failures=2):
        self.failures = failures
        self.calls = 0

    def reconcile(self, db, user, desired):
        self.calls += 1
        if self.calls <= self.failures:
            raise ISEError("Temporary ISE timeout")
        return super().reconcile(db, user, desired)


def active_user(clock):
    return setup_user(clock, assigned_at=clock.now - timedelta(minutes=2))


def test_failures_backoff_recovery_and_confirmation(clock):
    uid, _, _ = active_user(clock)
    adapter = FlakyISE()
    for count in range(3):
        assert run_once(adapter)
        with transaction() as db:
            state = db.get(EnforcementState, uid)
            if count < 2:
                assert state.confirmed is None
                assert state.status == "pending"
                job = db.scalar(select(Job))
                assert job.available_at > clock.now
                assert claim() is None
            else:
                assert state.status == "confirmed"
                assert state.confirmed_generation == state.generation
        clock.now += timedelta(seconds=10)
    with transaction() as db:
        assert [
            a.outcome for a in db.scalars(select(EnforcementAttempt).order_by(EnforcementAttempt.id))
        ] == ["failed", "failed", "mock_confirmed"]


def test_exhausted_burst_reconciles_later(clock):
    uid, _, _ = active_user(clock)
    broken = FlakyISE(failures=100)
    for _ in range(5):
        assert run_once(broken)
        clock.now += timedelta(seconds=60)
    assert not run_once(broken)
    with transaction() as db:
        assert db.get(EnforcementState, uid).status == "failed"
    clock.now += timedelta(minutes=6)
    with transaction() as db:
        reconcile_jobs(db, clock.now)
    assert run_once(MockISE())
    with transaction() as db:
        assert db.get(EnforcementState, uid).status == "confirmed"
        assert db.scalar(select(func.count()).select_from(EnforcementAttempt)) == 6


def test_stale_job_never_overrides_new_decision(clock):
    uid, _, _ = active_user(clock)
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, upload=100))
    adapter = FlakyISE(failures=0)
    assert run_once(adapter)  # Old NORMAL decision is obsolete.
    assert adapter.calls == 0
    assert run_once(adapter)
    assert adapter.calls == 1
    with transaction() as db:
        state = db.get(EnforcementState, uid)
        assert state.confirmed["mode"] == "LIMITED"
        assert db.scalar(select(Job).where(Job.id == 1)).status == "stale"


def test_delayed_job_reevaluates_after_rollover(clock):
    uid, _, _ = active_user(clock)
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, upload=200))
    clock.now = clock.now.replace(hour=0) + timedelta(days=1)
    adapter = FlakyISE(failures=0)
    while run_once(adapter):
        pass
    with transaction() as db:
        state = db.get(EnforcementState, uid)
        assert state.confirmed["mode"] == "NORMAL"
        assert adapter.calls == 1


def test_queue_claims_are_exclusive_and_leases_recover(clock):
    with transaction() as db:
        for i in range(20):
            enqueue(db, "evaluate", str(i))
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: claim(), range(20)))
    assert len({item[0] for item in claims}) == 20
    assert claim() is None
    clock.now += timedelta(minutes=6)
    replacement = claim()
    original = next(item for item in claims if item[0] == replacement[0])
    assert replacement[1] != original[1]
    process(original)  # Original worker cannot use its expired claim.
    with transaction() as db:
        assert db.get(Job, replacement[0]).status == "running"


def test_mock_confirmation_is_not_live_confirmation(clock, monkeypatch):
    uid, _, _ = active_user(clock)
    assert run_once(MockISE())
    monkeypatch.setattr(settings(), "ise_mode", "live")
    with transaction() as db:
        evaluate(db, uid)
        state = db.get(EnforcementState, uid)
        assert state.adapter == "live"
        assert state.confirmed_adapter == "mock"
        assert state.confirmed_generation != state.generation
    assert run_once()  # Missing live configuration must fail without claiming confirmation.
    with transaction() as db:
        state = db.get(EnforcementState, uid)
        assert state.status == "pending"
        assert state.confirmed_adapter == "mock"
        assert state.confirmed_generation != state.generation
