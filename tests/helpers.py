from datetime import timedelta

from bandwidth.db import transaction
from bandwidth.engine import assign
from bandwidth.models import Policy, PolicyVersion, User
from bandwidth.schemas import Measurement, PolicySpec


def spec(**overrides):
    return PolicySpec.model_validate(
        {
            "name": "Test policy",
            "quota_bytes": 100,
            "normal_download_kbps": 20000,
            "normal_upload_kbps": 10000,
            "limited_download_kbps": 2000,
            "limited_upload_kbps": 1000,
            "period_type": "daily",
            **overrides,
        }
    )


def setup_user(clock, policy_spec=None, assigned_at=None):
    s = policy_spec or spec()
    with transaction() as db:
        policy = Policy(name=s.name)
        db.add(policy)
        db.flush()
        version = PolicyVersion(policy_id=policy.id, revision=1, spec=s.model_dump(mode="json"))
        user = User(username="alice")
        db.add_all([version, user])
        db.flush()
        assign(db, user.id, version.id, True, "immediate", assigned_at or clock.now - timedelta(days=2))
        return user.id, policy.id, version.id


def measurement(clock, record_id="r1", upload=0, download=0, **overrides):
    return Measurement.model_validate(
        {
            "record_id": record_id,
            "username": "alice",
            "session_id": "s1",
            "observed_at": clock.now - timedelta(minutes=1),
            "kind": "delta",
            "upload": upload,
            "download": download,
            **overrides,
        }
    )
