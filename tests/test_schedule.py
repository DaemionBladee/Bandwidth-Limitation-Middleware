from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from bandwidth.schedule import window
from tests.helpers import spec


def dt(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "policy,instant,start,end",
    [
        (
            {"period_type": "daily", "timezone": "Asia/Yangon"},
            "2026-10-20T18:00:00+00:00",
            "2026-10-20T17:30:00+00:00",
            "2026-10-21T17:30:00+00:00",
        ),
        (
            {"period_type": "weekly", "weekly_day": 0},
            "2026-10-20T12:00:00+00:00",
            "2026-10-19T00:00:00+00:00",
            "2026-10-26T00:00:00+00:00",
        ),
        (
            {"period_type": "monthly", "monthly_day": 31},
            "2026-02-28T12:00:00+00:00",
            "2026-02-28T00:00:00+00:00",
            "2026-03-31T00:00:00+00:00",
        ),
        (
            {"period_type": "monthly", "monthly_day": 31},
            "2028-02-29T00:00:00+00:00",
            "2028-02-29T00:00:00+00:00",
            "2028-03-31T00:00:00+00:00",
        ),
        (
            {"period_type": "monthly", "monthly_day": 31},
            "2026-02-27T12:00:00+00:00",
            "2026-01-31T00:00:00+00:00",
            "2026-02-28T00:00:00+00:00",
        ),
        (
            {"period_type": "daily", "timezone": "America/New_York"},
            "2026-03-08T12:00:00+00:00",
            "2026-03-08T05:00:00+00:00",
            "2026-03-09T04:00:00+00:00",
        ),
        (
            {"period_type": "daily", "timezone": "America/New_York"},
            "2026-11-01T12:00:00+00:00",
            "2026-11-01T04:00:00+00:00",
            "2026-11-02T05:00:00+00:00",
        ),
        (
            {"period_type": "daily", "timezone": "America/New_York", "reset_time": "02:30"},
            "2026-03-08T08:00:00+00:00",
            "2026-03-08T07:30:00+00:00",
            "2026-03-09T06:30:00+00:00",
        ),
        (
            {"period_type": "daily", "timezone": "America/New_York", "reset_time": "01:30"},
            "2026-11-01T06:45:00+00:00",
            "2026-11-01T05:30:00+00:00",
            "2026-11-02T06:30:00+00:00",
        ),
    ],
)
def test_calendar_boundaries(policy, instant, start, end):
    bounds = window(spec(**policy), dt(instant))
    assert bounds.start == dt(start)
    assert bounds.end == dt(end)
    assert window(spec(**policy), bounds.end).start == bounds.end


def test_custom_expiry_and_repeat():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    one = spec(
        period_type="custom",
        custom_start=start,
        custom_end=start + timedelta(hours=1),
        expiry_action="LIMITED",
    )
    assert window(one, start - timedelta(seconds=1)).next_boundary == start
    assert window(one, start).start == start
    assert window(one, start + timedelta(hours=1)).inactive_mode == "LIMITED"
    assert window(one, start + timedelta(hours=1)).next_boundary is None
    repeat = one.model_copy(update={"repeat_interval_seconds": 7200})
    assert window(repeat, start + timedelta(minutes=90)).next_boundary == start + timedelta(hours=2)
    assert window(repeat, start + timedelta(hours=2)).start == start + timedelta(hours=2)


@pytest.mark.parametrize(
    "override",
    [
        {"quota_bytes": 0},
        {"normal_upload_kbps": -1},
        {"limited_download_kbps": 20001},
        {"limited_upload_kbps": 10001},
        {"timezone": "not/a/zone"},
        {"monthly_day": 32},
        {"reset_time": "01:00:01"},
        {
            "period_type": "custom",
            "custom_start": "2026-01-01T00:00:00Z",
            "custom_end": "2026-01-02T00:00:00Z",
        },
        {
            "period_type": "custom",
            "custom_start": "2026-01-01T00:00:00",
            "custom_end": "2026-01-02T00:00:00",
            "expiry_action": "NORMAL",
        },
        {
            "period_type": "custom",
            "custom_start": "2026-01-01T00:00:00Z",
            "custom_end": "2026-01-02T00:00:00Z",
            "repeat_interval_seconds": 3600,
        },
    ],
)
def test_policy_validation(override):
    with pytest.raises(ValidationError):
        spec(**override)
