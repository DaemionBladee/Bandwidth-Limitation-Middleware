"""Audited, administrator-only login-account CRUD with session revocation."""

from fastapi import HTTPException
from sqlalchemy import delete, func, select, text

from bandwidth.auth import COOKIE, digest, effective_permissions, password_hash
from bandwidth.db import utcnow
from bandwidth.models import Admin, AdminSession, Audit


def account_view(account):
    return {
        "id": account.id,
        "username": account.username,
        "role": account.role,
        "permissions": effective_permissions(account),
        "enabled": account.enabled,
        "revision": account.revision,
    }


def account_views(db):
    return [account_view(account) for account in db.scalars(select(Admin).order_by(Admin.id))]


def lock_account_management(db, request):
    # Serialize writes, including deletion/demotion of different administrators,
    # so two concurrent operations cannot remove the final enabled admin.
    db.execute(text("SELECT pg_advisory_xact_lock(923740126)"))
    token = request.cookies.get(COOKIE, "")
    session = db.get(AdminSession, digest(token)) if token else None
    account = db.get(Admin, session.admin_id) if session and session.expires_at > utcnow() else None
    if not account or not account.enabled or account.role != "admin":
        raise HTTPException(403, "A current administrator session is required")
    return account.username


def create_account(db, request, body, legacy=False):
    actor = lock_account_management(db, request)
    account = Admin(
        username=body.username,
        password_hash=password_hash(body.password),
        role="admin" if legacy else body.role,
        permissions=["read"] if legacy else body.permissions,
        enabled=True if legacy else body.enabled,
    )
    db.add(account)
    db.flush()
    result = account_view(account)
    db.add(
        Audit(
            actor=actor,
            action="admin.create" if legacy else "account.create",
            entity=f"account:{account.id}",
            after={"username": account.username} if legacy else result,
        )
    )
    return result


def manage_account(db, request, account_id, body, remove=False):
    actor = lock_account_management(db, request)
    account = db.get(Admin, account_id)
    if not account:
        raise HTTPException(404, "Account not found")
    if account.revision != body.expected_revision:
        raise HTTPException(409, "Account changed; review its latest version")
    losing_admin = (
        account.role == "admin" and account.enabled and (remove or body.role != "admin" or not body.enabled)
    )
    if (
        losing_admin
        and db.scalar(
            select(func.count()).select_from(Admin).where(Admin.role == "admin", Admin.enabled.is_(True))
        )
        <= 1
    ):
        raise HTTPException(409, "Keep at least one enabled administrator account")
    before = account_view(account)
    db.execute(delete(AdminSession).where(AdminSession.admin_id == account.id))
    if remove:
        db.delete(account)
        after = None
    else:
        account.username, account.role = body.username, body.role
        account.permissions, account.enabled = body.permissions, body.enabled
        account.revision += 1
        if body.password is not None:
            account.password_hash = password_hash(body.password)
        db.flush()
        after = account_view(account)
    db.add(
        Audit(
            actor=actor,
            action="account.delete" if remove else "account.update",
            entity=f"account:{account_id}",
            before=before,
            after={**after, "credentials_updated": body.password is not None} if after else None,
        )
    )
    return {"deleted": True, "id": account_id} if remove else after
