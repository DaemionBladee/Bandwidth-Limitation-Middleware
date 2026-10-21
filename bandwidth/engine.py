from sqlalchemy import func, select

from bandwidth.config import settings
from bandwidth.db import utcnow
from bandwidth.models import (
    AccountingRecord,
    Assignment,
    Audit,
    EnforcementState,
    Period,
    PolicyVersion,
    UsageReset,
    User,
)
from bandwidth.queue import enqueue
from bandwidth.schedule import Window, window
from bandwidth.schemas import PolicySpec


def lock_user(db, user_id):
    user = db.scalar(select(User).where(User.id == user_id).with_for_update())
    if not user:
        raise ValueError("Unknown user")
    return user


def effective_assignment(db, user_id, at):
    return db.scalar(
        select(Assignment)
        .where(
            Assignment.user_id == user_id,
            Assignment.cancelled.is_(False),
            Assignment.effective_from <= at,
            (Assignment.effective_until.is_(None)) | (Assignment.effective_until > at),
        )
        .order_by(Assignment.effective_from.desc(), Assignment.id.desc())
        .limit(1)
    )


def assignment_window(spec, assignment, at):
    bounds = window(spec, at)
    # Next-period schedule edits receive a fresh allowance even if calendar windows overlap.
    if bounds.start and assignment.fresh_from and bounds.start < assignment.fresh_from < bounds.end:
        return Window(assignment.fresh_from, bounds.end, bounds.next_boundary, bounds.inactive_mode)
    return bounds


def ensure_period(db, user_id, version, bounds):
    if bounds.start is None:
        return None
    period = db.scalar(
        select(Period).where(
            Period.user_id == user_id,
            Period.version_id == version.id,
            Period.starts_at == bounds.start,
            Period.ends_at == bounds.end,
        )
    )
    if not period:
        totals = db.execute(
            select(
                func.coalesce(func.sum(AccountingRecord.upload_bytes), 0),
                func.coalesce(func.sum(AccountingRecord.download_bytes), 0),
            ).where(
                AccountingRecord.user_id == user_id,
                AccountingRecord.observed_at >= bounds.start,
                AccountingRecord.observed_at < bounds.end,
            )
        ).one()
        period = Period(
            user_id=user_id,
            version_id=version.id,
            starts_at=bounds.start,
            ends_at=bounds.end,
            limit_bytes=version.spec["quota_bytes"],
            upload_bytes=int(totals[0]),
            download_bytes=int(totals[1]),
        )
        db.add(period)
        db.flush()
    return period


def quota_window(db, user_id, spec, assignment, at):
    bounds = assignment_window(spec, assignment, at)
    if not bounds.start:
        return bounds
    resets = db.scalars(
        select(UsageReset.effective_at)
        .where(
            UsageReset.user_id == user_id,
            UsageReset.effective_at > bounds.start,
            UsageReset.effective_at < bounds.end,
        )
        .order_by(UsageReset.effective_at)
    ).all()
    start, end = bounds.start, bounds.end
    for boundary in resets:
        if boundary <= at:
            start = boundary
        else:
            end = boundary
            break
    return Window(start, end, end, bounds.inactive_mode)


def evaluate(db, user_id, at=None):
    now = at or utcnow()
    lock_user(db, user_id)
    assignment = effective_assignment(db, user_id, now)
    if not assignment:
        return None, None, None
    version = db.get(PolicyVersion, assignment.version_id)
    spec = PolicySpec.model_validate(version.spec)
    bounds = quota_window(db, user_id, spec, assignment, now)
    period = ensure_period(db, user_id, version, bounds)
    if not assignment.enabled or not spec.enabled:
        mode = "NORMAL"
    elif period:
        mode = "LIMITED" if period.used_bytes >= spec.quota_bytes else "NORMAL"
    else:
        mode = bounds.inactive_mode
    target = {
        "mode": mode,
        "download_kbps": spec.normal_download_kbps if mode == "NORMAL" else spec.limited_download_kbps,
        "upload_kbps": spec.normal_upload_kbps if mode == "NORMAL" else spec.limited_upload_kbps,
        "period_id": period.id if period else None,
        "version_id": version.id,
    }
    adapter = settings().ise_mode
    state = db.get(EnforcementState, user_id)
    if not state:
        state = EnforcementState(user_id=user_id, generation=1, desired=target, adapter=adapter)
        db.add(state)
    elif state.desired != target or state.adapter != adapter:
        state.generation += 1
        state.desired = target
        state.adapter = adapter
        state.status = "pending"
        state.last_error = None
        state.updated_at = now
    db.flush()
    if state.confirmed_generation != state.generation or state.status != "confirmed":
        enqueue(
            db,
            "enforce",
            f"enforce:{user_id}:{state.generation}",
            {"user_id": user_id, "generation": state.generation},
        )
    return assignment, period, bounds


def assign(db, user_id, version_id, enabled, application, at=None):
    now = at or utcnow()
    lock_user(db, user_id)
    current = effective_assignment(db, user_id, now)
    effective = now
    if application == "next_period":
        if not current:
            raise ValueError("A next-period change requires an existing assignment")
        bounds = assignment_window(
            PolicySpec.model_validate(db.get(PolicyVersion, current.version_id).spec), current, now
        )
        if bounds.next_boundary is None:
            raise ValueError("This one-time interval has expired; choose immediate application")
        effective = bounds.next_boundary
    # Retain cancelled scheduled revisions as history; replace pending changes explicitly.
    pending = db.scalars(
        select(Assignment).where(
            Assignment.user_id == user_id,
            Assignment.cancelled.is_(False),
            Assignment.effective_from > now,
        )
    ).all()
    for row in pending:
        row.cancelled = True
    if current:
        current.effective_until = effective
    revision = Assignment(
        user_id=user_id,
        version_id=version_id,
        enabled=enabled,
        effective_from=effective,
        fresh_from=effective if application == "next_period" else current.fresh_from if current else None,
    )
    db.add(revision)
    db.flush()
    evaluate(db, user_id, now)
    return revision


class UsageResetConflict(ValueError):
    pass


def reset_usage(db, user_id, body, actor, at=None):
    now = at or utcnow()
    lock_user(db, user_id)
    request_id = str(body.request_id)
    existing = db.scalar(
        select(UsageReset).where(
            UsageReset.user_id == user_id,
            UsageReset.request_id == request_id,
        )
    )
    if existing:
        if existing.previous_period_id != body.expected_period_id or existing.reason != body.reason:
            raise UsageResetConflict("This reset request ID was already used with different parameters")
        return existing
    _, period, bounds = evaluate(db, user_id, now)
    if period is None:
        raise ValueError("A usage reset requires an active quota period")
    if period.id != body.expected_period_id:
        raise UsageResetConflict("The quota period changed. Review the latest usage and try again")
    if now <= period.starts_at:
        raise UsageResetConflict("This allowance has just started; wait a moment before resetting it")
    before = {
        "period_id": period.id,
        "used_bytes": period.used_bytes,
        "upload_bytes": period.upload_bytes,
        "download_bytes": period.download_bytes,
        "starts_at": period.starts_at.isoformat(),
        "ends_at": period.ends_at.isoformat(),
    }
    # Close ongoing views, retaining every accounting record and all completed totals.
    for row in db.scalars(
        select(Period).where(
            Period.user_id == user_id,
            Period.starts_at < now,
            Period.ends_at > now,
        )
    ):
        row.ends_at = now
    reset = UsageReset(
        user_id=user_id,
        previous_period_id=period.id,
        request_id=request_id,
        effective_at=now,
        actor=actor,
        reason=body.reason,
    )
    db.add(reset)
    db.flush()
    _, fresh, _ = evaluate(db, user_id, now)
    reset.new_period_id = fresh.id
    db.add(
        Audit(
            actor=actor,
            action="usage.reset",
            entity=f"user:{user_id}",
            before=before,
            after={
                "reset_id": reset.id,
                "period_id": fresh.id,
                "reason": body.reason,
                "effective_at": now.isoformat(),
                "used_bytes": fresh.used_bytes,
                "next_reset": bounds.next_boundary.isoformat(),
            },
        )
    )
    db.flush()
    return reset
