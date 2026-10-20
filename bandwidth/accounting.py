"""Source adapters and a transactional, idempotent accounting ledger."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert

from bandwidth.config import settings
from bandwidth.db import utcnow
from bandwidth.engine import assignment_window, effective_assignment, ensure_period, evaluate, lock_user
from bandwidth.models import AccountingRecord, Checkpoint, Inbox, Period, PolicyVersion, User
from bandwidth.schemas import Measurement, PolicySpec


class AccountingSource(Protocol):
    name: str

    def fetch(self, db, cursor: dict, limit: int) -> tuple[list[Measurement], dict]: ...


class MockAccounting:
    name = "mock"

    def fetch(self, db, cursor, limit):
        rows = db.scalars(
            select(Inbox).where(Inbox.id > cursor.get("id", 0)).order_by(Inbox.id).limit(limit)
        ).all()
        return [Measurement.model_validate(r.record) for r in rows], {
            "id": rows[-1].id if rows else cursor.get("id", 0)
        }


def radius_measurement(raw):
    """NAS input = subscriber upload, NAS output = subscriber download; explicit event ID is required."""
    return Measurement(
        record_id=raw["record_id"],
        username=raw["User-Name"],
        session_id=raw.get("Acct-Unique-Session-Id") or raw["Acct-Session-Id"],
        observed_at=datetime.fromtimestamp(raw["Event-Timestamp"], UTC),
        kind="cumulative",
        upload=int(raw["Acct-Input-Octets"]) + (int(raw.get("Acct-Input-Gigawords", 0)) << 32),
        download=int(raw["Acct-Output-Octets"]) + (int(raw.get("Acct-Output-Gigawords", 0)) << 32),
    )


class JSONLAccounting:
    def __init__(self, path, radius=False):
        self.path = Path(path)
        self.radius = radius
        self.name = "radius-jsonl" if radius else "jsonl"

    def fetch(self, db, cursor, limit):
        # Cursor and ingestion commit together. Refuse silent rotation/truncation;
        # an exporter should retain this file or an operator must reset the cursor.
        with self.path.open("rb") as stream:
            stat = self.path.stat()
            identity = f"{stat.st_dev}:{stat.st_ino}"
            if cursor and (cursor.get("identity") != identity or stat.st_size < cursor.get("offset", 0)):
                raise ValueError(
                    "Accounting file rotated/truncated; retain the old file and reconcile checkpoint"
                )
            stream.seek(cursor.get("offset", 0))
            records = []
            for _ in range(limit):
                position = stream.tell()
                line = stream.readline(65537)
                if len(line) > 65536:
                    raise ValueError("Accounting line exceeds 64 KiB")
                if not line or not line.endswith(b"\n"):
                    stream.seek(position)
                    break
                raw = json.loads(line)
                records.append(radius_measurement(raw) if self.radius else Measurement.model_validate(raw))
            return records, {"identity": identity, "offset": stream.tell()}


def source_adapter():
    config = settings()
    if config.accounting_source == "mock":
        return MockAccounting()
    if config.accounting_source in ("jsonl", "radius-jsonl") and config.accounting_file:
        return JSONLAccounting(config.accounting_file, config.accounting_source == "radius-jsonl")
    raise ValueError("Configure ACCOUNTING_SOURCE=mock, jsonl, or radius-jsonl and ACCOUNTING_FILE")


def difference(current, previous):
    return current - previous if current >= previous else current


def adjust_periods(db, record, upload_delta, download_delta):
    # Also correct completed periods and superseded policy views when a delayed
    # cumulative sample changes the subsequent sample's contribution.
    for period in db.scalars(
        select(Period).where(
            Period.user_id == record.user_id,
            Period.starts_at <= record.observed_at,
            Period.ends_at > record.observed_at,
        )
    ):
        period.upload_bytes += upload_delta
        period.download_bytes += download_delta


def ingest_record(db, source, measurement: Measurement, now=None):
    now = now or utcnow()
    if measurement.observed_at > now:
        raise ValueError("Future accounting timestamps are not accepted")
    up, down = measurement.counters()
    canonical = measurement.model_dump(mode="json")
    canonical.update(
        upload=up,
        download=down,
        unit="bytes",
        observed_at=measurement.observed_at.astimezone(UTC).isoformat(),
    )
    fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()
    user = db.scalar(select(User).where(User.username == measurement.username))
    if not user:
        raise ValueError(f"Accounting user is not registered: {measurement.username}")
    lock_user(db, user.id)
    existing = db.scalar(
        select(AccountingRecord).where(
            AccountingRecord.source == source,
            AccountingRecord.external_id == measurement.record_id,
        )
    )
    if existing:
        if existing.fingerprint != fingerprint:
            raise ValueError("Accounting record ID reused with different content")
        return False
    previous = successor = None
    if measurement.kind == "cumulative":
        base = select(AccountingRecord).where(
            AccountingRecord.user_id == user.id,
            AccountingRecord.source == source,
            AccountingRecord.session_id == measurement.session_id,
            AccountingRecord.kind == "cumulative",
        )
        key = tuple_(AccountingRecord.observed_at, AccountingRecord.external_id)
        position = (measurement.observed_at, measurement.record_id)
        previous = db.scalar(
            base.where(key < position)
            .order_by(AccountingRecord.observed_at.desc(), AccountingRecord.external_id.desc())
            .limit(1)
        )
        successor = db.scalar(
            base.where(key > position)
            .order_by(AccountingRecord.observed_at, AccountingRecord.external_id)
            .limit(1)
        )
        delta_up = difference(up, previous.upload_counter if previous else 0)
        delta_down = difference(down, previous.download_counter if previous else 0)
    else:
        delta_up, delta_down = up, down
    record = AccountingRecord(
        source=source,
        external_id=measurement.record_id,
        user_id=user.id,
        session_id=measurement.session_id,
        kind=measurement.kind,
        observed_at=measurement.observed_at,
        upload_counter=up,
        download_counter=down,
        upload_bytes=delta_up,
        download_bytes=delta_down,
        fingerprint=fingerprint,
    )
    adjust_periods(db, record, delta_up, delta_down)
    db.add(record)
    if successor:
        successor_up = difference(successor.upload_counter, up)
        successor_down = difference(successor.download_counter, down)
        adjust_periods(
            db, successor, successor_up - successor.upload_bytes, successor_down - successor.download_bytes
        )
        successor.upload_bytes, successor.download_bytes = successor_up, successor_down
    db.flush()
    # Materialize the historic effective period even if no scheduler was running then.
    assignment = effective_assignment(db, user.id, measurement.observed_at)
    if assignment:
        version = db.get(PolicyVersion, assignment.version_id)
        ensure_period(
            db,
            user.id,
            version,
            assignment_window(PolicySpec.model_validate(version.spec), assignment, measurement.observed_at),
        )
    evaluate(db, user.id, now)
    return True


def ingest_batch(db, adapter=None, limit=250):
    adapter = adapter or source_adapter()
    db.execute(insert(Checkpoint).values(source=adapter.name, cursor={}).on_conflict_do_nothing())
    checkpoint = db.scalar(select(Checkpoint).where(Checkpoint.source == adapter.name).with_for_update())
    records, cursor = adapter.fetch(db, checkpoint.cursor, limit)
    # A stable lock order prevents deadlocks between a multi-user batch and policy edits.
    names = {r.username for r in records}
    for user_id in db.scalars(select(User.id).where(User.username.in_(names)).order_by(User.id)):
        lock_user(db, user_id)
    count = sum(ingest_record(db, adapter.name, record) for record in records)
    checkpoint.cursor, checkpoint.updated_at = cursor, utcnow()
    return count
