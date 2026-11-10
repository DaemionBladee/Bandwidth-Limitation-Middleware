import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from bandwidth import accounting, cli, worker
from bandwidth.accounting import RsyslogAccounting, ingest_batch
from bandwidth.api import app
from bandwidth.config import Settings
from bandwidth.db import transaction
from bandwidth.models import AccountingRecord, Audit, Checkpoint, DefaultPlan, EnforcementState, User
from bandwidth.syslog import SyslogParser
from tests.helpers import setup_user, spec

FIXTURE = Path(__file__).parent / "fixtures" / "rsyslog" / "synthetic-accounting.jsonl"


def fields(clock, **changes):
    return {
        "User-Name": "alice",
        "Acct-Session-Id": "s1",
        "NAS-IP-Address": "192.0.2.10",
        "Acct-Status-Type": "Interim-Update",
        "Event-Timestamp": (clock.now - timedelta(minutes=1)).isoformat(),
        "Acct-Input-Octets": 25,
        "Acct-Output-Octets": 50,
        **changes,
    }


def csv(values):
    return ", ".join(f"{key}={value}" for key, value in values.items())


def envelope(message, sender="127.0.0.1", wire=True):
    if wire:
        message = (
            '<134>1 2026-10-20T11:59:00Z synthetic-ise fixture - - [fixture note="escaped\\]value"] '
            + message
        )
    return {"source_ip": sender, "hostname": "synthetic-ise", "raw_message": message}


def fragment(piece, index, count=2, identifier="synthetic-42"):
    return envelope(f"CISE_RADIUS_Accounting {identifier} {count} {index} {piece}")


def write(path, *rows, append=False):
    with path.open("a" if append else "w") as stream:
        for row in rows:
            stream.write(json.dumps(row) + "\n")


@pytest.mark.parametrize("style", ["json", "csv", "rfc3164", "cise", "rfc5424-app"])
def test_supported_formats_produce_same_accounting(clock, style):
    value = fields(clock)
    body = csv(value)
    if style == "json":
        message = envelope(json.dumps(value))
    elif style == "csv":
        message = envelope(body, wire=False)
    elif style == "rfc3164":
        message = envelope("<181>Oct 20 11:59:00 synthetic-ise acct: " + body, wire=False)
    elif style == "cise":
        message = envelope("CISE_RADIUS_Accounting synthetic-1 1 0 " + body)
    else:
        message = envelope(
            "<181>1 2026-10-20T11:59:00Z synthetic-ise CISE_RADIUS_Accounting - - - synthetic-1 1 0 " + body,
            wire=False,
        )
    parser = SyslogParser({}, "127.0.0.0/8", clock.now)
    result = parser.consume(message)
    expected = SyslogParser({}, "127.0.0.1", clock.now).consume(envelope(json.dumps(value)))
    assert result == expected
    assert result.counters() == (25, 50)


@pytest.mark.parametrize(
    "username,encoded",
    [
        ("ali,ce", '"ali,ce"'),
        ("o'connor", "o'connor"),
        ('ali"ce,team', '"ali""ce,team"'),
        ("domain\\alice", "domain\\alice"),
    ],
)
def test_csv_identity_quoting(clock, username, encoded):
    value = fields(clock)
    value["User-Name"] = encoded
    record = SyslogParser({}, "127.0.0.1", clock.now).consume(envelope(csv(value)))
    assert record.username == username


def test_event_time_fallbacks_never_use_receipt_time(clock):
    value = fields(clock)
    del value["Event-Timestamp"]
    parser = SyslogParser({}, "127.0.0.1", clock.now)
    assert parser.consume(envelope(csv(value))).observed_at.isoformat() == "2026-10-20T11:59:00+00:00"
    body = (
        "CISE_RADIUS_Accounting synthetic-1 1 0 2026-10-20 18:29:00.000 +06:30 0001 NOTICE Accounting: "
        + csv(value)
    )
    assert parser.consume(envelope(body)).observed_at.isoformat() == "2026-10-20T11:59:00+00:00"
    with pytest.raises(ValueError, match="Missing accounting event timestamp"):
        parser.consume({**envelope(csv(value), wire=False), "received_at": clock.now.isoformat()})


@pytest.mark.parametrize(
    "changes",
    [
        {"Acct-Input-Octets": -1},
        {"Acct-Input-Octets": True},
        {"Acct-Input-Octets": 1.5},
        {"Acct-Input-Octets": 2**32, "Acct-Input-Gigawords": 1},
        {"Event-Timestamp": "2026-10-20T11:59:00"},
        {"Event-Timestamp": True},
        {"Acct-Status-Type": "invented"},
        {"User-Name": ""},
    ],
)
def test_malformed_accounting_is_rejected(clock, changes):
    with pytest.raises(ValueError):
        SyslogParser({}, "127.0.0.1", clock.now).consume(envelope(json.dumps(fields(clock, **changes))))


def test_start_zero_counters_nas_events_and_nonaccounting(clock):
    value = fields(clock, **{"Acct-Status-Type": "Start"})
    del value["Acct-Input-Octets"], value["Acct-Output-Octets"]
    parser = SyslogParser({}, "127.0.0.1", clock.now)
    assert parser.consume(envelope(json.dumps(value))).counters() == (0, 0)
    for status in ["Interim-Update", "Stop"]:
        with pytest.raises(ValueError, match="missing Acct-Input-Octets"):
            parser.consume(envelope(json.dumps({**value, "Acct-Status-Type": status})))
    for body in [
        "Receiver connectivity test",
        "CISE_Passed_Authentications example 1 0 User-Name=alice",
        "Acct-Status-Type=Accounting-On",
        '{"Acct-Status-Type":8}',
    ]:
        assert parser.consume(envelope(body)) is None
    assert parser.stats["ignored"] == 4


def test_gigawords_aliases_and_conflicting_fields(clock):
    value = fields(clock, **{"Acct-Input-Octets": 4, "Acct-Input-Gigawords": 1})
    value["UserName"] = value.pop("User-Name")
    parser = SyslogParser({}, "127.0.0.1", clock.now)
    assert parser.consume(envelope(json.dumps(value))).upload == 2**32 + 4
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        parser.consume(envelope(csv(value) + ", User-Name=other"))
    with pytest.raises(ValueError, match="Malformed accounting JSON"):
        parser.consume(envelope('{"Acct-Status-Type":"Start","Acct-Status-Type":"Stop"}'))


def test_sender_validation_and_nas_namespace(clock):
    parser = SyslogParser({}, "192.0.2.0/24,::1", clock.now)
    first = parser.consume(envelope(csv(fields(clock)), sender="192.0.2.1"))
    second = parser.consume(envelope(csv(fields(clock, **{"NAS-IP-Address": "192.0.2.11"})), sender="::1"))
    assert first.session_id != second.session_id
    with pytest.raises(ValueError, match="RSYSLOG_ALLOWED_SENDERS"):
        parser.consume(envelope(csv(fields(clock))))
    value = fields(clock)
    del value["NAS-IP-Address"]
    with pytest.raises(ValueError, match="NAS identity"):
        parser.consume(envelope(json.dumps(value), sender="::1"))
    value["Acct-Unique-Session-Id"] = "globally-unique-s1"
    assert parser.consume(envelope(json.dumps(value), sender="::1")).session_id == "globally-unique-s1"


def test_receipt_details_and_cise_ids_do_not_change_record_identity(clock):
    body = csv(fields(clock))
    parser = SyslogParser({}, "127.0.0.0/8", clock.now)
    a = parser.consume(envelope("CISE_RADIUS_Accounting msg-a 1 0 " + body))
    b = parser.consume(
        {
            **envelope("CISE_RADIUS_Accounting msg-b 1 0 " + body, sender="127.0.0.2"),
            "received_at": "2099-01-01T00:00:00Z",
        }
    )
    assert a.record_id == b.record_id


def test_fragments_out_of_order_survive_checkpoint_and_duplicate_replays(clock, tmp_path):
    setup_user(clock)
    path = tmp_path / "raw.jsonl"
    body = csv(fields(clock))
    pieces = [body[: len(body) // 2], body[len(body) // 2 :]]
    write(path, fragment(pieces[1], 1))
    with transaction() as db:
        assert ingest_batch(db, RsyslogAccounting(path)) == 0
        cursor = db.get(Checkpoint, "rsyslog").cursor
        assert cursor["pending_count"] == 1
    write(path, fragment(pieces[1], 1), fragment(pieces[0], 0), fragment(pieces[0], 0), append=True)
    with transaction() as db:
        assert ingest_batch(db, RsyslogAccounting(path)) == 1
        cursor = db.get(Checkpoint, "rsyslog").cursor
        assert cursor["pending_count"] == 0
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 1
    response = TestClient(app).get("/api/status", headers={"Authorization": "Bearer test-automation-token"})
    checkpoint = response.json()["checkpoints"][0]
    assert checkpoint["parser"]["parsed"] == 1
    assert "pending" not in checkpoint and "completed" not in checkpoint


def test_conflicting_fragment_is_atomic_and_cursor_is_not_mutated(clock, tmp_path):
    path = tmp_path / "raw.jsonl"
    write(path, fragment("User-Name=private-fixture, ", 0))
    adapter = RsyslogAccounting(path)
    _, cursor = adapter.fetch(None, {}, 1)
    before = json.dumps(cursor, sort_keys=True)
    write(path, fragment("conflicting-private-fixture", 0), append=True)
    with pytest.raises(ValueError, match="Conflicting duplicate CISE") as error:
        adapter.fetch(None, cursor, 1)
    assert "private-fixture" not in str(error.value)
    assert json.dumps(cursor, sort_keys=True) == before


def test_missing_fragment_timeout_recovers_when_late_data_arrives(clock, tmp_path):
    setup_user(clock)
    path = tmp_path / "raw.jsonl"
    body = csv(fields(clock))
    write(path, fragment(body[:20], 0))
    with transaction() as db:
        ingest_batch(db, RsyslogAccounting(path, fragment_timeout=1))
    clock.now += timedelta(seconds=2)
    with pytest.raises(ValueError, match="fragments are incomplete"):
        with transaction() as db:
            ingest_batch(db, RsyslogAccounting(path, fragment_timeout=1))
    write(path, envelope("Receiver test"), fragment(body[20:], 1), append=True)
    with transaction() as db:
        assert ingest_batch(db, RsyslogAccounting(path, fragment_timeout=1), limit=1) == 0
    with transaction() as db:
        assert ingest_batch(db, RsyslogAccounting(path, fragment_timeout=1), limit=1) == 1


def test_partial_lines_and_malformed_batch_rollback(clock, tmp_path):
    setup_user(clock)
    path = tmp_path / "raw.jsonl"
    line = json.dumps(envelope(json.dumps(fields(clock))))
    path.write_text(line)
    adapter = RsyslogAccounting(path)
    with transaction() as db:
        assert ingest_batch(db, adapter) == 0
        assert db.get(Checkpoint, "rsyslog").cursor["offset"] == 0
    with path.open("a") as stream:
        stream.write('\n{"raw_message":"sensitive-fixture","source_ip":"not-an-ip"}\n')
    with pytest.raises(ValueError, match="source_ip") as error:
        with transaction() as db:
            ingest_batch(db, adapter)
    assert "sensitive-fixture" not in str(error.value)
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 0
        assert db.get(Checkpoint, "rsyslog").cursor["offset"] == 0


def test_worker_discovers_default_subscriber_and_limits_with_synthetic_fixture(clock, monkeypatch):
    _, policy_id, _ = setup_user(clock, spec())
    with transaction() as db:
        db.get(DefaultPlan, 1).policy_id = policy_id
    config = Settings(accounting_source="rsyslog", accounting_file=str(FIXTURE))
    monkeypatch.setattr(accounting, "settings", lambda: config)
    worker.schedule()
    for _ in range(30):
        if not worker.run_once():
            break
    with transaction() as db:
        user = db.scalar(select(User).where(User.username == "synthetic-subscriber"))
        assert user is not None
        state = db.get(EnforcementState, user.id)
        assert state.desired["mode"] == "LIMITED"
        assert state.status == "confirmed" and state.confirmed_adapter == "mock"
        records = list(db.scalars(select(AccountingRecord).where(AccountingRecord.user_id == user.id)))
        assert sum(r.upload_bytes + r.download_bytes for r in records) == 110
        assert db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "user.discover")) == 1


def test_duplicate_usage_and_concurrent_readers(clock, tmp_path):
    setup_user(clock)
    path = tmp_path / "raw.jsonl"
    row = envelope(json.dumps(fields(clock)))
    write(path, row, row)

    def read(_):
        with transaction() as db:
            return ingest_batch(db, RsyslogAccounting(path))

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(read, range(2))) == 1
    with transaction() as db:
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 1


def test_preflight_cli_reads_fixture_without_writing_accounting(capsys):
    cli.check_syslog(str(FIXTURE))
    output = json.loads(capsys.readouterr().out)
    assert output["parsed"] >= 3 and output["ignored"] >= 1
    assert output["pending_count"] == 0
    with transaction() as db:
        assert db.get(Checkpoint, "rsyslog") is None
        assert db.scalar(select(func.count()).select_from(AccountingRecord)) == 0


def test_preflight_cli_reports_incomplete_capture(clock, tmp_path, capsys):
    path = tmp_path / "raw.jsonl"
    write(path, fragment("first part", 0))
    with pytest.raises(SystemExit, match="Capture is incomplete"):
        cli.check_syslog(str(path))
    assert json.loads(capsys.readouterr().out)["pending_count"] == 1


def test_fragment_zero_header_sets_fallback_time_even_when_arriving_last(clock):
    value = fields(clock)
    del value["Event-Timestamp"]
    body = csv(value)
    split = len(body) // 2
    parser = SyslogParser({}, "127.0.0.1", clock.now)
    first = fragment(body[:split], 0)
    last = fragment(body[split:], 1)
    first["raw_message"] = first["raw_message"].replace("11:59:00Z", "11:58:00Z")
    assert parser.consume(last) is None
    record = parser.consume(first)
    assert record.observed_at.isoformat() == "2026-10-20T11:58:00+00:00"


@pytest.mark.parametrize("count,index", [(0, 0), (65, 0), (2, 2), (2, -1)])
def test_invalid_fragment_bounds_are_rejected(clock, count, index):
    with pytest.raises(ValueError, match="fragment"):
        SyslogParser({}, "127.0.0.1", clock.now).consume(fragment("piece", index, count=count))


def test_fragment_resource_limits_and_payload_errors_are_sanitized(clock, tmp_path):
    parser = SyslogParser({}, "127.0.0.1", clock.now)
    for index in range(128):
        parser.consume(fragment("piece", 0, identifier=f"synthetic-{index}"))
    with pytest.raises(ValueError, match="Too many incomplete"):
        parser.consume(fragment("piece", 0, identifier="synthetic-overflow"))
    path = tmp_path / "raw.jsonl"
    path.write_text('{"private-fixture":' + "x" * 1_048_576)
    with pytest.raises(ValueError, match="line exceeds") as error:
        RsyslogAccounting(path).fetch(None, {}, 1)
    assert "private-fixture" not in str(error.value)
    path.write_text('{"raw_message":"private-fixture",}\n')
    with pytest.raises(ValueError, match="Malformed rsyslog") as error:
        RsyslogAccounting(path).fetch(None, {}, 1)
    assert "private-fixture" not in str(error.value)


def test_rotation_is_rejected_and_unknown_subscriber_rolls_back(clock, tmp_path):
    path = tmp_path / "raw.jsonl"
    write(path, envelope(json.dumps(fields(clock))))
    adapter = RsyslogAccounting(path)
    with pytest.raises(ValueError, match="not registered"):
        with transaction() as db:
            ingest_batch(db, adapter)
    with transaction() as db:
        assert db.get(Checkpoint, "rsyslog") is None
    setup_user(clock)
    with transaction() as db:
        ingest_batch(db, adapter)
        cursor = db.get(Checkpoint, "rsyslog").cursor
    path.rename(tmp_path / "retained.jsonl")
    write(path, envelope(json.dumps(fields(clock))))
    with pytest.raises(ValueError, match="rotated/truncated"):
        adapter.fetch(None, cursor, 250)
