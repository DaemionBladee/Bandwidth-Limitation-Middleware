"""Consistent dashboard snapshots and cross-process PostgreSQL event delivery."""

import json
import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import anyio
import psycopg
from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, text

from bandwidth.auth import require_admin
from bandwidth.db import engine, transaction, utcnow
from bandwidth.models import AccountingRecord, User

log = logging.getLogger(__name__)
TIMEZONES = sorted(available_timezones() - {"localtime", "Factory"})


def validate_timezone(name):
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("Select a valid IANA timezone") from exc


def dashboard_snapshot(timezone="UTC", now=None):
    # Import serializers after the API has been initialized; route handlers and
    # the event stream use the same representations and authorization boundary.
    from bandwidth.api import audit_views, policy_views, status_view, user_view

    now = now or utcnow()
    zone = validate_timezone(timezone)
    first_day = now.astimezone(zone).date() - timedelta(days=6)
    first_at = datetime.combine(first_day, time(), zone).astimezone(UTC)
    local_day = func.date_trunc("day", func.timezone(timezone, AccountingRecord.observed_at))
    with transaction() as db:
        db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        days = {
            day.date().isoformat(): (int(upload), int(download))
            for day, upload, download in db.execute(
                select(
                    local_day,
                    func.sum(AccountingRecord.upload_bytes),
                    func.sum(AccountingRecord.download_bytes),
                )
                .where(AccountingRecord.observed_at >= first_at, AccountingRecord.observed_at <= now)
                .group_by(local_day)
                .order_by(local_day)
            )
        }
        return jsonable_encoder(
            {
                "users": [user_view(db, row, now) for row in db.scalars(select(User).order_by(User.id))],
                "policies": policy_views(db),
                "status": status_view(db),
                "audit": audit_views(db),
                "traffic": [
                    {
                        "day": (day := first_day + timedelta(days=i)).isoformat(),
                        "upload_bytes": days.get(day.isoformat(), (0, 0))[0],
                        "download_bytes": days.get(day.isoformat(), (0, 0))[1],
                    }
                    for i in range(7)
                ],
                "timezone": timezone,
            }
        )


async def event_response(request, timezone):
    validate_timezone(timezone)
    # A dedicated LISTEN connection per active dashboard. No application pool
    # connection is held while waiting, and notifications are delivered only at commit.
    dsn = engine.url.render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    try:
        connection = await psycopg.AsyncConnection.connect(dsn, autocommit=True, connect_timeout=5)
        await connection.execute("LISTEN bandwidth_dashboard")
    except psycopg.Error as exc:
        if "connection" in locals():
            await connection.close()
        raise HTTPException(503, "Live updates are temporarily unavailable") from exc

    async def events():
        previous = None
        try:
            yield "retry: 2500\n\n"
            while not await request.is_disconnected():
                try:
                    await anyio.to_thread.run_sync(require_admin, request)
                except HTTPException:
                    yield "event: session-expired\ndata: {}\n\n"
                    return
                snapshot = await anyio.to_thread.run_sync(dashboard_snapshot, timezone)
                encoded = json.dumps(snapshot, separators=(",", ":"), sort_keys=True)
                if encoded != previous:
                    yield f"event: dashboard\ndata: {encoded}\n\n"
                    previous = encoded
                else:
                    yield ": heartbeat\n\n"
                # Recheck session revocation/expiry at least every 15 seconds;
                # reconnects always receive a full snapshot, so no events are lost.
                async for _ in connection.notifies(timeout=15, stop_after=1):
                    pass
                await anyio.sleep(0.15)  # coalesce bursts from worker transactions
        except (psycopg.Error, OSError):
            log.warning("Dashboard event stream interrupted; client will reconnect")
        finally:
            with anyio.CancelScope(shield=True):
                await connection.close()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
