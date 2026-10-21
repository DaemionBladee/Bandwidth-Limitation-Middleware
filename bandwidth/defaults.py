"""Default selection and first-registration provisioning, in the caller's transaction."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from bandwidth.engine import assign
from bandwidth.models import Audit, DefaultPlan, Policy, PolicyVersion, User
from bandwidth.schemas import PolicySpec, UserCreate


def lock_default(db, shared=False):
    # A permanent singleton, seeded by the migration. Removal clears its pointer;
    # the increasing revision and lock remain to reject stale confirmations.
    return db.scalar(select(DefaultPlan).where(DefaultPlan.id == 1).with_for_update(read=shared))


def latest(db, policy_id):
    return db.scalar(
        select(PolicyVersion)
        .where(PolicyVersion.policy_id == policy_id)
        .order_by(PolicyVersion.revision.desc())
        .limit(1)
    )


def default_view(db):
    row = db.get(DefaultPlan, 1)
    version = latest(db, row.policy_id) if row and row.policy_id else None
    return {
        "configured": version is not None,
        "revision": row.revision if row else 0,
        "policy_id": row.policy_id if row else None,
        "policy_revision": version.revision if version else None,
        "spec": version.spec if version else None,
    }


def provisioning_version(db):
    row = lock_default(db, shared=True)
    if not row or not row.policy_id:
        return None
    # All policy editors take the default lock first, then policy, then user.
    db.scalar(select(Policy).where(Policy.id == row.policy_id).with_for_update(read=True))
    version = latest(db, row.policy_id)
    return version if version.spec["enabled"] else None


def register(db, username, actor, at, version=None, discovered=False):
    username = UserCreate(username=username).username
    user_id = db.scalar(
        insert(User)
        .values(username=username)
        .on_conflict_do_nothing(index_elements=["username"])
        .returning(User.id)
    )
    if user_id is None:
        return db.scalar(select(User).where(User.username == username)), False
    user = db.get(User, user_id)
    db.add(
        Audit(
            actor=actor,
            action="user.discover" if discovered else "user.create",
            entity=f"user:{user.id}",
            after={"username": username},
        )
    )
    if version:
        spec = PolicySpec.model_validate(version.spec)
        assign(db, user.id, version.id, True, "immediate", at)
        db.add(
            Audit(
                actor=actor,
                action="assignment.default",
                entity=f"user:{user.id}",
                after={
                    "username": username,
                    "policy_id": version.policy_id,
                    "policy_name": spec.name,
                    "quota_bytes": spec.quota_bytes,
                    "effective_from": at.isoformat(),
                },
            )
        )
    return user, True


def discover(db, measurements, source, now):
    version = provisioning_version(db)
    names = {r.username for r in measurements}
    existing = set(db.scalars(select(User.username).where(User.username.in_(names))))
    missing = names - existing
    if missing and not version:
        raise ValueError(
            f"Accounting user is not registered: {sorted(missing)[0]}; configure an enabled default plan or register the user"
        )
    first_seen = {}
    for row in measurements:
        if row.observed_at > now:
            raise ValueError("Future accounting timestamps are not accepted")
        first_seen[row.username] = min(first_seen.get(row.username, row.observed_at), row.observed_at)
    for username in sorted(missing):
        register(db, username, f"accounting:{source}", first_seen[username], version, discovered=True)
