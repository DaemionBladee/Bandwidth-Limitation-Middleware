from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bandwidth.db import Base, utcnow


class Policy(Base):
    __tablename__ = "policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PolicyVersion(Base):
    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("policy_id", "revision"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    policy_id: Mapped[int] = mapped_column(ForeignKey("policies.id"))
    revision: Mapped[int] = mapped_column(Integer)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (Index("ix_assignment_effective", "user_id", "effective_from"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("policy_versions.id"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fresh_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled: Mapped[bool] = mapped_column(Boolean, default=False)


class Period(Base):
    __tablename__ = "quota_periods"
    __table_args__ = (
        UniqueConstraint("user_id", "version_id", "starts_at", "ends_at"),
        CheckConstraint("upload_bytes >= 0 AND download_bytes >= 0"),
        CheckConstraint("ends_at > starts_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    version_id: Mapped[int] = mapped_column(ForeignKey("policy_versions.id"))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    limit_bytes: Mapped[int] = mapped_column(BigInteger)
    upload_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    download_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def used_bytes(self):
        return self.upload_bytes + self.download_bytes


class AccountingRecord(Base):
    __tablename__ = "accounting_records"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index("ix_accounting_session", "user_id", "source", "session_id", "observed_at", "external_id"),
        CheckConstraint("upload_bytes >= 0 AND download_bytes >= 0"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(80))
    external_id: Mapped[str] = mapped_column(String(250))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    session_id: Mapped[str] = mapped_column(String(250))
    kind: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    upload_counter: Mapped[int] = mapped_column(BigInteger)
    download_counter: Mapped[int] = mapped_column(BigInteger)
    upload_bytes: Mapped[int] = mapped_column(BigInteger)
    download_bytes: Mapped[int] = mapped_column(BigInteger)
    fingerprint: Mapped[str] = mapped_column(String(64))


class Checkpoint(Base):
    __tablename__ = "accounting_checkpoints"
    source: Mapped[str] = mapped_column(String(80), primary_key=True)
    cursor: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Inbox(Base):
    __tablename__ = "mock_accounting_inbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    record: Mapped[dict] = mapped_column(JSONB)


class EnforcementState(Base):
    __tablename__ = "enforcement_states"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    desired: Mapped[dict] = mapped_column(JSONB)
    confirmed: Mapped[dict | None] = mapped_column(JSONB)
    confirmed_generation: Mapped[int | None] = mapped_column(Integer)
    confirmed_adapter: Mapped[str | None] = mapped_column(String(20))
    adapter: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "status", "available_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)


class EnforcementAttempt(Base):
    __tablename__ = "enforcement_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    generation: Mapped[int] = mapped_column(Integer)
    adapter: Mapped[str] = mapped_column(String(20))
    desired: Mapped[dict] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MockISEState(Base):
    __tablename__ = "mock_ise_states"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    applied: Mapped[dict] = mapped_column(JSONB)


class Audit(Base):
    __tablename__ = "admin_audit"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(200))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(200), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admins.id"))
    csrf: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginThrottle(Base):
    __tablename__ = "login_throttle"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
