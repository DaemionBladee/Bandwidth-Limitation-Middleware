import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from bandwidth.accounting import JSONLAccounting, ingest_batch, ingest_record, radius_measurement
from bandwidth.db import transaction
from bandwidth.engine import evaluate
from bandwidth.models import AccountingRecord, Checkpoint, EnforcementState, Inbox, Period
from tests.helpers import measurement, setup_user, spec


@pytest.mark.parametrize("used,expected", [(99, "NORMAL"), (100, "LIMITED"), (101, "LIMITED")])
def test_quota_threshold(clock, used, expected):
    user_id, _, _ = setup_user(clock)
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, upload=1, download=used - 1))
        _, period, _ = evaluate(db, user_id, clock.now)
        assert period.used_bytes == used
        assert db.get(EnforcementState, user_id).desired["mode"] == expected


def test_duplicate_and_conflicting_id(clock):
    user_id, _, _ = setup_user(clock)
    record = measurement(clock, upload=25, download=25)
    with transaction() as db:
        assert ingest_record(db, "test", record)
        assert not ingest_record(db, "test", record)
    with pytest.raises(ValueError, match="reused"):
        with transaction() as db:
            ingest_record(db, "test", record.model_copy(update={"upload": 26}))
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 1
        assert evaluate(db, user_id)[1].used_bytes == 50


def test_out_of_order_counter_resets_and_new_session(clock):
    user_id, _, _ = setup_user(clock)
    samples = [("a", 100, 200, 10), ("b", 10, 20, 9), ("c", 40, 50, 8)]
    for index in [2, 0, 1]:
        name, up, down, minute = samples[index]
        with transaction() as db:
            ingest_record(
                db,
                "test",
                measurement(
                    clock,
                    name,
                    up,
                    down,
                    kind="cumulative",
                    observed_at=clock.now - timedelta(minutes=minute),
                ),
            )
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, "new-session", 7, 8, kind="cumulative", session_id="s2"))
        period = evaluate(db, user_id)[1]
        assert (period.upload_bytes, period.download_bytes) == (147, 258)


def test_late_sample_corrects_completed_period_and_successor(clock):
    user_id, _, _ = setup_user(clock, spec(quota_bytes=1000))
    yesterday = clock.now.replace(hour=23) - timedelta(days=1)
    for rid, up, at in [
        ("a", 50, yesterday - timedelta(hours=1)),
        ("c", 100, clock.now - timedelta(hours=1)),
    ]:
        with transaction() as db:
            ingest_record(db, "test", measurement(clock, rid, up, kind="cumulative", observed_at=at))
    with transaction() as db:
        old = db.scalar(
            select(Period).where(Period.user_id == user_id, Period.ends_at == clock.now.replace(hour=0))
        )
        old_id = old.id
        assert old.used_bytes == 50
    with transaction() as db:
        ingest_record(db, "test", measurement(clock, "b", 70, kind="cumulative", observed_at=yesterday))
        assert db.get(Period, old_id).used_bytes == 70
        assert evaluate(db, user_id)[1].used_bytes == 30


def test_concurrent_usage_and_duplicates(clock):
    user_id, _, _ = setup_user(clock, spec(quota_bytes=10000))

    def send(i):
        with transaction() as db:
            return ingest_record(db, "test", measurement(clock, f"r{i % 20}", upload=2, download=3))

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert sum(pool.map(send, range(40))) == 20
    with transaction() as db:
        assert evaluate(db, user_id)[1].used_bytes == 100


def test_checkpoint_is_atomic_with_records(clock):
    setup_user(clock)
    good = measurement(clock, "good", upload=10)
    bad = measurement(clock, "bad", username="missing")
    with transaction() as db:
        db.add_all([Inbox(record=r.model_dump(mode="json")) for r in [good, bad]])
    with pytest.raises(ValueError, match="not registered"):
        with transaction() as db:
            ingest_batch(db)
    with transaction() as db:
        assert db.get(Checkpoint, "mock") is None
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 0
    with transaction() as db:
        badrow = db.scalar(select(Inbox).where(Inbox.id == 2))
        badrow.record = measurement(clock, "good2", upload=20).model_dump(mode="json")
    with transaction() as db:
        assert ingest_batch(db) == 2
        assert db.get(Checkpoint, "mock").cursor == {"id": 2}
        assert ingest_batch(db) == 0


def test_jsonl_units_partial_lines_and_rotation(clock, tmp_path):
    user_id, _, _ = setup_user(clock, spec(quota_bytes=10**9))
    path = tmp_path / "accounting.jsonl"
    record = measurement(clock, upload=2, download=3, unit="KiB")
    path.write_text(json.dumps(record.model_dump(mode="json")))
    adapter = JSONLAccounting(path)
    with transaction() as db:
        assert ingest_batch(db, adapter) == 0
    with path.open("a") as stream:
        stream.write("\n")
    with transaction() as db:
        assert ingest_batch(db, adapter) == 1
        assert evaluate(db, user_id)[1].used_bytes == 5 * 1024
    path.write_text("")
    with pytest.raises(ValueError, match="rotated/truncated"):
        with transaction() as db:
            ingest_batch(db, adapter)


def test_radius_gigawords(clock):
    record = radius_measurement(
        {
            "record_id": "r",
            "User-Name": "alice",
            "Acct-Session-Id": "s",
            "Event-Timestamp": clock.now.timestamp(),
            "Acct-Input-Octets": 4,
            "Acct-Input-Gigawords": 1,
            "Acct-Output-Octets": 8,
        }
    )
    assert record.counters() == (2**32 + 4, 8)


def test_future_and_unknown_users_do_not_count(clock):
    setup_user(clock)
    for record in [
        measurement(clock, observed_at=clock.now + timedelta(seconds=1)),
        measurement(clock, username="unknown"),
    ]:
        with pytest.raises(ValueError):
            with transaction() as db:
                ingest_record(db, "test", record)
