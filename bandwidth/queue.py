import secrets
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert

from bandwidth.db import transaction, utcnow
from bandwidth.models import Job


def enqueue(db, kind, key, payload=None, at=None):
    # Avoid waiting on an update to an existing queue row while holding a user
    # lock; workers acquire their job row before that same user lock.
    if db.scalar(select(Job.id).where(Job.dedupe_key == key)):
        return
    db.execute(
        insert(Job)
        .values(
            kind=kind,
            dedupe_key=key,
            payload=payload or {},
            status="pending",
            attempts=0,
            available_at=at or utcnow(),
        )
        .on_conflict_do_nothing(index_elements=[Job.dedupe_key])
    )


def claim(now=None):
    now = now or utcnow()
    with transaction() as db:
        job = db.scalar(
            select(Job)
            .where(
                Job.available_at <= now,
                or_(Job.status == "pending", (Job.status == "running") & (Job.lease_until < now)),
            )
            .order_by(Job.available_at, Job.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not job:
            return None
        job.status = "running"
        job.lease_until = now + timedelta(minutes=5)
        job.lease_token = secrets.token_hex(16)
        return job.id, job.lease_token
