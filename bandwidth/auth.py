import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from bandwidth.config import settings
from bandwidth.db import transaction, utcnow
from bandwidth.models import Admin, AdminSession, LoginThrottle
from bandwidth.permissions import ALL_PERMISSIONS

COOKIE = "bandwidth_session"


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${value}"


def password_matches(password, encoded):
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)
    except ValueError:
        return False


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin admin request rejected")


def effective_permissions(account):
    return (
        list(ALL_PERMISSIONS)
        if account.role == "admin"
        else sorted({"read", *(p for p in account.permissions if p in ALL_PERMISSIONS)})
    )


@dataclass(frozen=True)
class Principal:
    id: int | None
    username: str
    role: str
    permissions: list[str]
    revision: int

    def public(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "permissions": self.permissions,
            "revision": self.revision,
        }


def authenticate(request: Request):
    config = settings()
    auth = request.headers.get("authorization", "")
    if (
        config.admin_api_token
        and auth.startswith("Bearer ")
        and hmac.compare_digest(auth[7:], config.admin_api_token)
    ):
        principal = Principal(None, "api-admin", "admin", list(ALL_PERMISSIONS), 0)
    else:
        token = request.cookies.get(COOKIE, "")
        with transaction() as db:
            session = db.get(AdminSession, digest(token)) if token else None
            if not session or session.expires_at <= utcnow():
                raise HTTPException(401, "Account authentication required")
            account = db.get(Admin, session.admin_id)
            if not account or not account.enabled:
                raise HTTPException(401, "Account authentication required")
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                check_origin(request)
                if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
                    raise HTTPException(403, "Invalid CSRF token")
            principal = Principal(
                account.id, account.username, account.role, effective_permissions(account), account.revision
            )
    request.state.principal = principal
    return principal


def require_reader(request: Request):
    return authenticate(request).username


def require_admin(request: Request):
    principal = authenticate(request)
    if principal.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return principal.username


def require_permission(code):
    if code not in ALL_PERMISSIONS:
        raise ValueError("Unknown permission")

    def dependency(request: Request):
        principal = authenticate(request)
        if principal.role != "admin" and code not in principal.permissions:
            raise HTTPException(403, f"Permission required: {code}")
        return principal.username

    return dependency


def login(request, username, password):
    check_origin(request)
    key = digest(f"{request.client.host if request.client else 'unknown'}:{username}")
    now = utcnow()
    result = None
    with transaction() as db:
        db.execute(
            insert(LoginThrottle).values(key=key, failures=0, window_start=now).on_conflict_do_nothing()
        )
        throttle = db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
        if throttle.window_start < now - timedelta(minutes=5):
            throttle.failures, throttle.window_start = 0, now
        if throttle.failures >= 10:
            raise HTTPException(429, "Too many login attempts; retry in five minutes")
        admin = db.scalar(select(Admin).where(Admin.username == username))
        # Equal scrypt work for unknown users.
        valid = password_matches(password, admin.password_hash if admin else "scrypt$unknown$" + "0" * 128)
        if admin and admin.enabled and valid:
            throttle.failures = 0
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.add(
                AdminSession(
                    token_hash=digest(token),
                    admin_id=admin.id,
                    csrf=csrf,
                    expires_at=now + timedelta(hours=settings().session_hours),
                )
            )
            result = token, csrf
        else:
            throttle.failures += 1
    if not result:
        raise HTTPException(401, "Invalid account credentials")
    return result


def confirm_admin_password(request, password):
    """Sensitive account management requires the acting human's session and password."""
    check_origin(request)
    now, valid = utcnow(), False
    actor = None
    with transaction() as db:
        token = request.cookies.get(COOKIE, "")
        session = db.get(AdminSession, digest(token)) if token else None
        if not session or session.expires_at <= now:
            raise HTTPException(403, "Sign in with an administrator account to manage login accounts")
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session.csrf):
            raise HTTPException(403, "Invalid CSRF token")
        admin = db.get(Admin, session.admin_id)
        if not admin or not admin.enabled:
            raise HTTPException(401, "Account authentication required")
        if admin.role != "admin":
            raise HTTPException(403, "Administrator role required")
        key = digest(f"account-management:{admin.id}")
        db.execute(
            insert(LoginThrottle).values(key=key, failures=0, window_start=now).on_conflict_do_nothing()
        )
        throttle = db.scalar(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())
        if throttle.window_start < now - timedelta(minutes=5):
            throttle.failures, throttle.window_start = 0, now
        if throttle.failures >= 5:
            raise HTTPException(429, "Too many password confirmations; retry in five minutes")
        valid = password_matches(password, admin.password_hash)
        if valid:
            throttle.failures = 0
            actor = admin.username
        else:
            throttle.failures += 1
    if not valid:
        raise HTTPException(403, "Your current administrator password is incorrect")
    return actor
