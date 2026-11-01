"""Durable jobs: leases, SKIP LOCKED claims, bounded bursts, and slow reconciliation."""

import logging
import time
from datetime import timedelta

from sqlalchemy import select

from bandwidth.accounting import ingest_batch
from bandwidth.config import settings
from bandwidth.db import transaction, utcnow
from bandwidth.engine import evaluate, lock_user
from bandwidth.ise import ise_adapter
from bandwidth.models import EnforcementAttempt, EnforcementState, Job, User
from bandwidth.queue import claim, enqueue

log = logging.getLogger(__name__)
PERIODIC = ("ingest", "evaluate", "rollover", "reconcile")


def schedule():
    with transaction() as db:
        for kind in PERIODIC:
            enqueue(db, kind, f"periodic:{kind}")


def enforce(db, job, adapter=None):
    user_id, generation = job.payload["user_id"], job.payload["generation"]
    user = lock_user(db, user_id)
    # Re-evaluate at execution time: a job queued before a reset/edit can never
    # apply its obsolete mode, including when the rollover worker was delayed.
    evaluate(db, user_id)
    state = db.get(EnforcementState, user_id)
    if not state or state.generation != generation:
        job.status = "stale"
        return
    attempt = EnforcementAttempt(
        user_id=user_id,
        generation=generation,
        adapter=state.adapter,
        desired=state.desired.copy(),
        outcome="pending",
    )
    db.add(attempt)
    try:
        active_adapter = adapter or ise_adapter()
        if active_adapter.name != state.adapter:
            raise ValueError("Enforcement adapter changed; reevaluation required")
        confirmed = active_adapter.reconcile(db, user, state.desired)
        if not confirmed:
            raise ValueError("ISE acknowledged the request; active session confirmation is pending")
        state.confirmed, state.confirmed_generation = state.desired.copy(), generation
        state.confirmed_adapter = active_adapter.name
        state.confirmed_at, state.status, state.last_error = utcnow(), "confirmed", None
        attempt.outcome = "mock_confirmed" if active_adapter.name == "mock" else "confirmed"
        job.status, job.last_error = "done", None
    except Exception as exc:
        # Adapter exceptions are deliberately sanitized: no upstream bodies/secrets.
        from bandwidth.ise import ISEError

        error = (
            str(exc) if isinstance(exc, (ISEError, ValueError)) else f"Adapter error ({type(exc).__name__})"
        )
        attempt.outcome, attempt.error = "failed", error
        job.attempts += 1
        state.last_error, job.last_error = error, error
        if job.attempts >= settings().enforcement_max_attempts:
            job.status, state.status = "failed", "failed"
        else:
            job.status, state.status = "pending", "pending"
            job.available_at = utcnow() + timedelta(seconds=min(300, 2**job.attempts))
    state.updated_at = utcnow()


def reconcile_jobs(db, now):
    # A new bounded burst every five minutes also probes confirmed states for drift.
    # Failed attempts remain immutable; the next burst has a distinct durable key.
    for user_id in db.scalars(select(User.id).order_by(User.id)):
        lock_user(db, user_id)
        evaluate(db, user_id, now)
        state = db.get(EnforcementState, user_id)
        if state and state.updated_at <= now - timedelta(minutes=5):
            enqueue(
                db,
                "enforce",
                f"reconcile:{user_id}:{state.generation}:{int(now.timestamp()) // 300}",
                {"user_id": user_id, "generation": state.generation},
            )


def process(claimed, adapter=None):
    job_id, token = claimed
    try:
        with transaction() as db:
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job.status != "running" or job.lease_token != token:
                return
            now = utcnow()
            if job.kind == "enforce":
                enforce(db, job, adapter)
            elif job.kind == "ingest":
                ingest_batch(db)
            elif job.kind in ("evaluate", "rollover"):
                for user_id in db.scalars(select(User.id).order_by(User.id)):
                    evaluate(db, user_id, now)
            elif job.kind == "reconcile":
                reconcile_jobs(db, now)
            else:
                raise ValueError("Unknown job kind")
            if job.kind in PERIODIC:
                job.status, job.last_error, job.attempts = "pending", None, 0
                job.available_at = utcnow() + timedelta(seconds=settings().worker_interval_seconds)
            job.lease_until, job.lease_token = None, None
    except Exception as exc:
        # A failed transaction rolls back both accounting and its checkpoint.
        log.error("Job %s failed (%s)", job_id, type(exc).__name__)
        with transaction() as db:
            job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if job.lease_token == token:
                detail = str(exc) if type(exc) is ValueError else type(exc).__name__
                job.status, job.last_error = "pending", f"Job failed: {detail}"
                job.attempts += 1
                job.available_at = utcnow() + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))
                job.lease_until, job.lease_token = None, None


def run_once(adapter=None):
    claimed = claim()
    if not claimed:
        return False
    process(claimed, adapter)
    return True


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    schedule()
    log.info("Bandwidth worker running; ISE adapter=%s", settings().ise_mode)
    while True:
        if not run_once():
            time.sleep(0.5)


if __name__ == "__main__":
    main()
