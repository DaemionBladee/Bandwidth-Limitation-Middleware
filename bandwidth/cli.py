import argparse
import getpass
import os

from dotenv import load_dotenv
from sqlalchemy import select

from bandwidth.auth import password_hash
from bandwidth.db import transaction
from bandwidth.engine import assign
from bandwidth.models import Admin, Audit, Policy, PolicyVersion, User
from bandwidth.schemas import PolicySpec

DEMO = PolicySpec(
    name="50 GB Monthly",
    quota_bytes=50_000_000_000,
    normal_download_kbps=20_000,
    normal_upload_kbps=10_000,
    limited_download_kbps=2_000,
    limited_upload_kbps=1_000,
    period_type="monthly",
    timezone="UTC",
    monthly_day=1,
)


def bootstrap(username="admin"):
    load_dotenv()
    with transaction() as db:
        if db.scalar(select(Admin).where(Admin.username == username)):
            print("Admin already exists; no credentials changed")
            return
        password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD") or getpass.getpass(
            "New admin password (12+ characters): "
        )
        if len(password) < 12:
            raise SystemExit("Use a password of at least 12 characters")
        db.add(Admin(username=username, password_hash=password_hash(password)))
        db.add(Audit(actor="bootstrap", action="admin.create", entity=username))
    print(f"Admin {username} created")


def demo():
    with transaction() as db:
        policy = db.scalar(select(Policy).where(Policy.name == DEMO.name))
        if not policy:
            policy = Policy(name=DEMO.name)
            db.add(policy)
            db.flush()
            version = PolicyVersion(policy_id=policy.id, revision=1, spec=DEMO.model_dump(mode="json"))
            db.add(version)
            db.flush()
            db.add(
                Audit(actor="demo", action="policy.create", entity=f"policy:{policy.id}", after=version.spec)
            )
        else:
            version = db.scalar(
                select(PolicyVersion)
                .where(PolicyVersion.policy_id == policy.id)
                .order_by(PolicyVersion.revision.desc())
                .limit(1)
            )
        user = db.scalar(select(User).where(User.username == "demo@example.com"))
        if not user:
            user = User(username="demo@example.com")
            db.add(user)
            db.flush()
            assign(db, user.id, version.id, True, "immediate")
            db.add(Audit(actor="demo", action="assignment.create", entity=f"user:{user.id}"))
    print("Demo policy and subscriber are present (decimal GB and Mbps)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["bootstrap", "demo"])
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    bootstrap(args.username) if args.command == "bootstrap" else demo()


if __name__ == "__main__":
    main()
