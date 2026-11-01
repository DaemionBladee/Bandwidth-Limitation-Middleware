"""Half-open periods. Calendar schedules use local time; custom repeats use elapsed UTC seconds."""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from bandwidth.schemas import PolicySpec


@dataclass(frozen=True)
class Window:
    start: datetime | None
    end: datetime | None
    next_boundary: datetime | None
    inactive_mode: str = "NORMAL"


def local_boundary(day: date, spec: PolicySpec) -> datetime:
    zone = ZoneInfo(spec.timezone)
    wall = datetime.combine(day, spec.reset_time)
    # Ambiguous wall times choose the first occurrence. Nonexistent times roll forward
    # by the DST gap (02:30 -> 03:30); all comparisons and intervals remain in UTC.
    return wall.replace(tzinfo=zone, fold=0).astimezone(UTC)


def month_day(year, month, spec):
    return date(year, month, min(spec.monthly_day, calendar.monthrange(year, month)[1]))


def adjacent_month(day, offset, spec):
    n = day.year * 12 + day.month - 1 + offset
    return month_day(n // 12, n % 12 + 1, spec)


def window(spec: PolicySpec, instant: datetime) -> Window:
    if instant.utcoffset() is None:
        raise ValueError("Schedule evaluation requires a timezone-aware timestamp")
    now = instant.astimezone(UTC)
    if spec.period_type == "custom":
        start, end = spec.custom_start.astimezone(UTC), spec.custom_end.astimezone(UTC)
        if now < start:
            return Window(None, None, start)
        if spec.repeat_interval_seconds:
            interval = timedelta(seconds=spec.repeat_interval_seconds)
            n = (now - start) // interval
            start, end = start + n * interval, end + n * interval
            if now >= end:
                return Window(None, None, start + interval, spec.expiry_action or "NORMAL")
            return Window(start, end, end)
        if now >= end:
            return Window(None, None, None, spec.expiry_action)
        return Window(start, end, end)
    today = now.astimezone(ZoneInfo(spec.timezone)).date()
    if spec.period_type == "daily":
        start_day = today if local_boundary(today, spec) <= now else today - timedelta(days=1)
        end_day = start_day + timedelta(days=1)
    elif spec.period_type == "weekly":
        start_day = today - timedelta(days=(today.weekday() - spec.weekly_day) % 7)
        if local_boundary(start_day, spec) > now:
            start_day -= timedelta(days=7)
        end_day = start_day + timedelta(days=7)
    else:
        start_day = month_day(today.year, today.month, spec)
        if local_boundary(start_day, spec) > now:
            start_day = adjacent_month(start_day, -1, spec)
        end_day = adjacent_month(start_day, 1, spec)
    start, end = local_boundary(start_day, spec), local_boundary(end_day, spec)
    return Window(start, end, end)
