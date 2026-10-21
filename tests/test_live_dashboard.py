from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from bandwidth.accounting import ingest_record
from bandwidth.api import app
from bandwidth.db import engine, transaction
from bandwidth.models import User
from tests.helpers import measurement, setup_user

AUTH = {"Authorization": "Bearer test-automation-token"}


def test_snapshot_and_timezones_require_admin():
    with TestClient(app) as client:
        for path in ("/api/dashboard", "/api/timezones", "/api/events"):
            assert client.get(path).status_code == 401
        zones = client.get("/api/timezones", headers=AUTH).json()
        assert "Asia/Yangon" in zones and "UTC" in zones
        assert client.get("/api/dashboard?timezone=wrong/zone", headers=AUTH).status_code == 422


def test_snapshot_aggregates_actual_traffic_in_selected_timezone(clock):
    setup_user(clock)
    # A record at 19:00 UTC falls on the next local day in Yangon.
    clock.now = clock.now.replace(hour=20)
    with transaction() as db:
        ingest_record(
            db,
            "test",
            measurement(clock, "r", upload=25, download=50, observed_at=clock.now - timedelta(hours=1)),
        )
    with TestClient(app) as client:
        utc = client.get("/api/dashboard?timezone=UTC", headers=AUTH).json()
        yangon = client.get("/api/dashboard?timezone=Asia/Yangon", headers=AUTH).json()
        assert utc["traffic"][-1] == {"day": "2026-10-20", "upload_bytes": 25, "download_bytes": 50}
        assert yangon["traffic"][-1] == {"day": "2026-10-21", "upload_bytes": 25, "download_bytes": 50}
        assert len(yangon["traffic"]) == 7
        assert yangon["users"][0]["used_bytes"] == 75
        assert yangon["users"][0]["period_id"] is not None


def test_dashboard_notification_is_emitted_only_after_commit():
    dsn = engine.url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    with psycopg.connect(dsn, autocommit=True) as listener:
        listener.execute("LISTEN bandwidth_dashboard")
        with pytest.raises(RuntimeError):
            with transaction() as db:
                db.add(User(username="rolled-back"))
                db.flush()
                raise RuntimeError("Rollback")
        assert list(listener.notifies(timeout=0.1, stop_after=1)) == []
        with transaction() as db:
            db.add(User(username="committed"))
        events = list(listener.notifies(timeout=2, stop_after=1))
        assert len(events) == 1 and events[0].channel == "bandwidth_dashboard"
