from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from bandwidth.auth import COOKIE, confirm_admin_password, digest, login, password_hash, require_admin
from bandwidth.config import settings
from bandwidth.dashboard import TIMEZONES, dashboard_snapshot, event_response
from bandwidth.db import transaction, utcnow
from bandwidth.defaults import default_view, lock_default, provisioning_version, register
from bandwidth.engine import (
    UsageResetConflict,
    assign,
    effective_assignment,
    evaluate,
    lock_user,
    quota_window,
    reset_usage,
)
from bandwidth.models import (
    Admin,
    AdminSession,
    Assignment,
    Audit,
    Checkpoint,
    EnforcementAttempt,
    EnforcementState,
    Inbox,
    Job,
    Period,
    Policy,
    PolicyVersion,
    UsageReset,
    User,
)
from bandwidth.queue import enqueue
from bandwidth.schemas import (
    AdminCreate,
    AssignmentInput,
    DefaultPlanEdit,
    DefaultPlanInput,
    Measurement,
    PolicyEdit,
    PolicySpec,
    ResetUsageInput,
    UserCreate,
)

app = FastAPI(title="Bandwidth Engine", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)
static = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static), name="static")
AdminOnly = Depends(require_admin)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    return JSONResponse(
        status_code=409, content={"detail": "Conflicting name or concurrent change; refresh and retry"}
    )


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default error includes the rejected input, potentially passwords.
    errors = [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


@app.exception_handler(UsageResetConflict)
async def stale_reset(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.get("/")
def dashboard():
    return FileResponse(static / "index.html")


@app.get("/health")
def health():
    with transaction() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok"}


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=512)


@app.post("/api/login")
def sign_in(body: LoginInput, request: Request, response: Response):
    token, csrf = login(request, body.username, body.password)
    response.set_cookie(
        COOKIE,
        token,
        httponly=True,
        samesite="strict",
        secure=settings().cookie_secure,
        max_age=settings().session_hours * 3600,
    )
    return {"csrf": csrf}


@app.get("/api/session")
def session_info(request: Request, actor=AdminOnly):
    with transaction() as db:
        session = db.get(AdminSession, digest(request.cookies.get(COOKIE, "")))
        return {"username": actor, "csrf": session.csrf if session else None}


@app.get("/api/timezones")
def timezones(actor=AdminOnly):
    return TIMEZONES


@app.get("/api/dashboard")
def dashboard_data(timezone: str = "UTC", actor=AdminOnly):
    return dashboard_snapshot(timezone, utcnow())


@app.get("/api/events")
async def live_events(request: Request, timezone: str = "UTC", actor=AdminOnly):
    return await event_response(request, timezone)


@app.post("/api/logout")
def sign_out(request: Request, response: Response, actor=AdminOnly):
    with transaction() as db:
        session = db.get(AdminSession, digest(request.cookies.get(COOKIE, "")))
        if session:
            db.delete(session)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/admins")
def administrators(actor=AdminOnly):
    with transaction() as db:
        return [{"id": a.id, "username": a.username} for a in db.scalars(select(Admin).order_by(Admin.id))]


@app.post("/api/admins", status_code=201)
def create_administrator(body: AdminCreate, request: Request, actor=AdminOnly):
    actor = confirm_admin_password(request, body.current_password)
    with transaction() as db:
        admin = Admin(username=body.username, password_hash=password_hash(body.password))
        db.add(admin)
        db.flush()
        audit(db, actor, "admin.create", f"admin:{admin.id}", after={"username": admin.username})
        return {"id": admin.id, "username": admin.username}


def audit(db, actor, action, entity, before=None, after=None):
    db.add(Audit(actor=actor, action=action, entity=entity, before=before, after=after))


def latest_version(db, policy_id):
    row = db.scalar(
        select(PolicyVersion)
        .where(PolicyVersion.policy_id == policy_id)
        .order_by(PolicyVersion.revision.desc())
        .limit(1)
    )
    if not row:
        raise HTTPException(404, "Policy not found")
    return row


def policy_views(db):
    return [
        {"id": row.id, "revision": (version := latest_version(db, row.id)).revision, "spec": version.spec}
        for row in db.scalars(select(Policy).order_by(Policy.id))
    ]


@app.get("/api/policies")
def policies(actor=AdminOnly):
    with transaction() as db:
        return policy_views(db)


@app.post("/api/policies", status_code=201)
def create_policy(spec: PolicySpec, actor=AdminOnly):
    with transaction() as db:
        policy = Policy(name=spec.name)
        db.add(policy)
        db.flush()
        db.add(PolicyVersion(policy_id=policy.id, revision=1, spec=spec.model_dump(mode="json")))
        audit(db, actor, "policy.create", f"policy:{policy.id}", after=spec.model_dump(mode="json"))
        return {"id": policy.id, "revision": 1}


@app.get("/api/default-plan")
def get_default_plan(actor=AdminOnly):
    with transaction() as db:
        return default_view(db)


def save_default(db, body, actor, create=False):
    row = lock_default(db)
    if create and row.policy_id is not None:
        raise HTTPException(409, "A default plan already exists; edit or remove it first")
    if not create and row.policy_id is None:
        raise HTTPException(404, "No default plan is configured")
    if not create and row.revision != body.expected_revision:
        raise HTTPException(409, "The default plan changed; review the latest version")
    before = default_view(db)
    if body.policy_id:
        policy = db.scalar(select(Policy).where(Policy.id == body.policy_id).with_for_update())
        if not policy:
            raise HTTPException(404, "Policy not found")
        latest_version(db, policy.id)
    elif create:
        policy = Policy(name=body.spec.name)
        db.add(policy)
        db.flush()
        db.add(PolicyVersion(policy_id=policy.id, revision=1, spec=body.spec.model_dump(mode="json")))
    else:
        policy = db.scalar(select(Policy).where(Policy.id == row.policy_id).with_for_update())
        version = latest_version(db, policy.id)
        policy.name = body.spec.name
        db.add(
            PolicyVersion(
                policy_id=policy.id, revision=version.revision + 1, spec=body.spec.model_dump(mode="json")
            )
        )
    row.policy_id, row.revision = policy.id, row.revision + 1
    db.flush()
    after = default_view(db)
    audit(db, actor, "default_plan.create" if create else "default_plan.edit", "default-plan", before, after)
    return after


@app.post("/api/default-plan", status_code=201)
def create_default_plan(body: DefaultPlanInput, actor=AdminOnly):
    with transaction() as db:
        return save_default(db, body, actor, create=True)


@app.put("/api/default-plan")
def update_default_plan(body: DefaultPlanEdit, actor=AdminOnly):
    with transaction() as db:
        return save_default(db, body, actor)


@app.delete("/api/default-plan")
def remove_default_plan(expected_revision: int = Query(ge=0), actor=AdminOnly):
    with transaction() as db:
        row = lock_default(db)
        if row.policy_id is None:
            raise HTTPException(404, "No default plan is configured")
        if row.revision != expected_revision:
            raise HTTPException(409, "The default plan changed; review the latest version")
        before = default_view(db)
        row.policy_id, row.revision = None, row.revision + 1
        db.flush()
        after = default_view(db)
        audit(db, actor, "default_plan.delete", "default-plan", before, after)
        return after


@app.put("/api/policies/{policy_id}")
def edit_policy(policy_id: int, body: PolicyEdit, actor=AdminOnly):
    now = utcnow()
    with transaction() as db:
        default = lock_default(db)
        policy = db.scalar(select(Policy).where(Policy.id == policy_id).with_for_update())
        if not policy:
            raise HTTPException(404, "Policy not found")
        old = latest_version(db, policy_id)
        if body.expected_revision != old.revision:
            raise HTTPException(409, "Policy revision changed; reload before editing")
        spec = body.spec.model_dump(mode="json")
        new = PolicyVersion(policy_id=policy_id, revision=old.revision + 1, spec=spec)
        db.add(new)
        policy.name = body.spec.name
        if default.policy_id == policy_id:
            default.revision += 1
        db.flush()
        ids = db.scalars(
            select(Assignment.user_id)
            .join(PolicyVersion, Assignment.version_id == PolicyVersion.id)
            .where(
                PolicyVersion.policy_id == policy_id,
                Assignment.cancelled.is_(False),
                Assignment.effective_from <= now,
                (Assignment.effective_until.is_(None)) | (Assignment.effective_until > now),
            )
            .distinct()
            .order_by(Assignment.user_id)
        ).all()
        for user_id in ids:
            lock_user(db, user_id)
            current = effective_assignment(db, user_id, now)
            if current and db.get(PolicyVersion, current.version_id).policy_id == policy_id:
                assign(db, user_id, new.id, current.enabled, body.application, now)
        audit(
            db,
            actor,
            "policy.edit",
            f"policy:{policy_id}",
            before=old.spec,
            after={"spec": spec, "application": body.application, "affected_users": ids},
        )
        return {"id": policy_id, "revision": new.revision, "affected_users": ids}


@app.post("/api/users", status_code=201)
def create_user(body: UserCreate, actor=AdminOnly):
    with transaction() as db:
        version = provisioning_version(db)
        user, created = register(db, body.username, actor, utcnow(), version)
        if not created:
            raise HTTPException(409, "A subscriber with this username already exists")
        return {"id": user.id, "username": user.username, "default_assigned": version is not None}


@app.put("/api/users/{user_id}/assignment")
def assignment(user_id: int, body: AssignmentInput, actor=AdminOnly):
    with transaction() as db:
        # Lock policy before user, consistently with policy edits.
        if not db.scalar(select(Policy).where(Policy.id == body.policy_id).with_for_update()):
            raise HTTPException(404, "Policy not found")
        if not db.get(User, user_id):
            raise HTTPException(404, "User not found")
        row = assign(db, user_id, latest_version(db, body.policy_id).id, body.enabled, body.application)
        audit(
            db,
            actor,
            "assignment.change",
            f"user:{user_id}",
            after={**body.model_dump(), "effective_from": row.effective_from.isoformat()},
        )
        return {"id": row.id, "effective_from": row.effective_from}


def user_view(db, user, now):
    assignment = effective_assignment(db, user.id, now)
    version = db.get(PolicyVersion, assignment.version_id) if assignment else None
    spec = PolicySpec.model_validate(version.spec) if version else None
    bounds = quota_window(db, user.id, spec, assignment, now) if spec else None
    period = (
        db.scalar(
            select(Period).where(
                Period.user_id == user.id,
                Period.version_id == version.id,
                Period.starts_at == bounds.start,
                Period.ends_at == bounds.end,
            )
        )
        if spec and bounds.start
        else None
    )
    state = db.get(EnforcementState, user.id)
    pending = db.scalars(
        select(Assignment)
        .where(
            Assignment.user_id == user.id, Assignment.cancelled.is_(False), Assignment.effective_from > now
        )
        .order_by(Assignment.effective_from)
    ).all()
    used = period.used_bytes if period else 0
    return {
        "id": user.id,
        "username": user.username,
        "policy": spec.name if spec else None,
        "policy_id": version.policy_id if version else None,
        "revision": version.revision if version else None,
        "enabled": assignment.enabled and spec.enabled if assignment else False,
        "used_bytes": used,
        "period_id": period.id if period else None,
        "period_starts_at": period.starts_at if period else None,
        "period_ends_at": period.ends_at if period else None,
        "upload_bytes": period.upload_bytes if period else 0,
        "download_bytes": period.download_bytes if period else 0,
        "quota_bytes": spec.quota_bytes if spec else None,
        "remaining_bytes": max(0, spec.quota_bytes - used) if spec and bounds.start else None,
        "next_reset": bounds.next_boundary if bounds else None,
        "timezone": spec.timezone if spec else None,
        "period_active": bool(bounds and bounds.start),
        "pending": [{"version_id": p.version_id, "effective_from": p.effective_from} for p in pending],
        "ise": {
            "adapter": state.adapter,
            "desired": state.desired,
            "confirmed": state.confirmed,
            "generation": state.generation,
            "confirmed_generation": state.confirmed_generation,
            "confirmed_adapter": state.confirmed_adapter,
            "status": state.status,
            "last_error": state.last_error,
            "confirmed_at": state.confirmed_at,
        }
        if state
        else None,
    }


@app.get("/api/users")
def users(actor=AdminOnly):
    with transaction() as db:
        return [user_view(db, row, utcnow()) for row in db.scalars(select(User).order_by(User.id))]


@app.get("/api/users/{user_id}/history")
def history(user_id: int, actor=AdminOnly):
    with transaction() as db:
        if not db.get(User, user_id):
            raise HTTPException(404, "Subscriber not found")
        return {
            "resets": [
                {
                    "id": r.id,
                    "effective_at": r.effective_at,
                    "actor": r.actor,
                    "reason": r.reason,
                    "previous_period_id": r.previous_period_id,
                    "new_period_id": r.new_period_id,
                }
                for r in db.scalars(
                    select(UsageReset)
                    .where(UsageReset.user_id == user_id)
                    .order_by(UsageReset.effective_at.desc())
                    .limit(100)
                )
            ],
            "periods": [
                {
                    "id": p.id,
                    "version_id": p.version_id,
                    "starts_at": p.starts_at,
                    "ends_at": p.ends_at,
                    "used_bytes": p.used_bytes,
                    "upload_bytes": p.upload_bytes,
                    "download_bytes": p.download_bytes,
                    "quota_bytes": p.limit_bytes,
                }
                for p in db.scalars(
                    select(Period)
                    .where(Period.user_id == user_id)
                    .order_by(Period.starts_at.desc(), Period.id.desc())
                    .limit(100)
                )
            ],
            "attempts": [
                {
                    "generation": a.generation,
                    "adapter": a.adapter,
                    "mode": a.desired["mode"],
                    "outcome": a.outcome,
                    "error": a.error,
                    "created_at": a.created_at,
                }
                for a in db.scalars(
                    select(EnforcementAttempt)
                    .where(EnforcementAttempt.user_id == user_id)
                    .order_by(EnforcementAttempt.id.desc())
                    .limit(100)
                )
            ],
        }


@app.post("/api/users/{user_id}/reconcile")
def retry(user_id: int, actor=AdminOnly):
    with transaction() as db:
        lock_user(db, user_id)
        evaluate(db, user_id)
        state = db.get(EnforcementState, user_id)
        if not state:
            raise HTTPException(422, "Assign a policy first")
        state.generation += 1
        state.status = "pending"
        enqueue(
            db,
            "enforce",
            f"enforce:{user_id}:{state.generation}",
            {"user_id": user_id, "generation": state.generation},
        )
        audit(db, actor, "enforcement.retry", f"user:{user_id}", after={"generation": state.generation})
        return {"generation": state.generation}


@app.post("/api/users/{user_id}/reset-usage")
def reset_subscriber_usage(user_id: int, body: ResetUsageInput, actor=AdminOnly):
    with transaction() as db:
        if not db.get(User, user_id):
            raise HTTPException(404, "Subscriber not found")
        reset = reset_usage(db, user_id, body, actor)
        return {
            "reset_id": reset.id,
            "effective_at": reset.effective_at,
            "period_id": reset.new_period_id,
            "user": user_view(db, db.get(User, user_id), utcnow()),
        }


@app.post("/api/mock/accounting", status_code=202)
def mock_accounting(body: Measurement, actor=AdminOnly):
    if settings().accounting_source != "mock":
        raise HTTPException(409, "Mock accounting is disabled")
    body.counters()
    if body.observed_at > utcnow():
        raise HTTPException(422, "Future accounting timestamps are not accepted")
    with transaction() as db:
        if not db.scalar(select(User).where(User.username == body.username)):
            plan = default_view(db)
            if not plan["configured"] or not plan["spec"]["enabled"]:
                raise HTTPException(
                    422, "Create this accounting user first or configure an enabled default plan"
                )
        db.add(Inbox(record=body.model_dump(mode="json")))
        audit(db, actor, "mock.accounting", f"record:{body.record_id}", after={"username": body.username})
    return {"status": "queued", "source": "mock"}


def status_view(db):
    return {
        "ise_mode": settings().ise_mode,
        "accounting_source": settings().accounting_source,
        "live_adapter_selected": settings().ise_mode == "live",
        "jobs": [
            {"kind": kind, "status": status, "count": count}
            for kind, status, count in db.execute(
                select(Job.kind, Job.status, func.count()).group_by(Job.kind, Job.status)
            )
        ],
        "checkpoints": [
            {"source": c.source, "updated_at": c.updated_at} for c in db.scalars(select(Checkpoint))
        ],
        "worker_errors": [
            {"kind": j.kind, "error": j.last_error, "attempts": j.attempts}
            for j in db.scalars(
                select(Job).where(Job.last_error.is_not(None)).order_by(Job.id.desc()).limit(20)
            )
        ],
    }


@app.get("/api/status")
def status(actor=AdminOnly):
    with transaction() as db:
        return status_view(db)


def audit_views(db):
    user_names = dict(db.execute(select(User.id, User.username)).all())
    policy_names = dict(db.execute(select(Policy.id, Policy.name)).all())

    def label(entity):
        kind, _, key = entity.partition(":")
        if key.isdigit():
            return {"user": user_names, "policy": policy_names}.get(kind, {}).get(int(key), entity)
        return "Default quota plan" if entity == "default-plan" else entity

    return [
        {
            "id": a.id,
            "actor": a.actor,
            "action": a.action,
            "entity": a.entity,
            "entity_label": label(a.entity),
            "before": a.before,
            "after": a.after,
            "created_at": a.created_at,
        }
        for a in db.scalars(select(Audit).order_by(Audit.id.desc()).limit(100))
    ]


@app.get("/api/audit")
def audit_history(actor=AdminOnly):
    with transaction() as db:
        return audit_views(db)
