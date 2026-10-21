# Admin API

All routes below require an admin cookie or `Authorization: Bearer <ADMIN_API_TOKEN>`, except login and health. Cookie mutations need `X-CSRF-Token` from login or `/api/session`. Inject bearer credentials securely; examples omit them.

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/api/login` | `{ "username": "admin", "password": "…" }`; HttpOnly cookie and CSRF token |
| GET / POST | `/api/session` / `/api/logout` | Current admin and CSRF / invalidate session |
| GET / POST | `/api/policies` | Latest specifications / create a policy |
| PUT | `/api/policies/{id}` | `{ "spec": {…}, "expected_revision": 1, "application": "immediate" }` |
| GET / POST | `/api/users` | Monitor subscribers / create `{ "username": "alice" }` |
| PUT | `/api/users/{id}/assignment` | `{ "policy_id": 1, "enabled": true, "application": "next_period" }` |
| GET | `/api/users/{id}/history` | Latest 100 periods and attempts |
| POST | `/api/users/{id}/reconcile` | Queue a new generation |
| POST | `/api/users/{id}/reset-usage` | Audited fresh allowance; requires current period confirmation and idempotency UUID |
| POST | `/api/mock/accounting` | Normalized measurement; only for mock accounting |
| GET | `/api/status` | Adapter selections, jobs, checkpoints, worker errors |
| GET | `/api/audit` | Latest 100 changes with before/after data |
| GET | `/api/timezones` | Available IANA timezone names |
| GET | `/api/dashboard?timezone=Asia/Yangon` | Consistent subscribers/policies/status/audit snapshot and seven local days of recorded traffic |
| GET | `/api/events?timezone=Asia/Yangon` | Authenticated SSE snapshots after committed changes; `dashboard` and `session-expired` events |
| GET | `/health` | Database connectivity (public, no data) |

Example policy:

```json
{
  "name": "50 GB Monthly", "enabled": true, "quota_bytes": 50000000000,
  "normal_download_kbps": 20000, "normal_upload_kbps": 10000,
  "limited_download_kbps": 2000, "limited_upload_kbps": 1000,
  "period_type": "monthly", "timezone": "Asia/Yangon", "reset_time": "00:00", "monthly_day": 1
}
```

Use `weekly_day: 0..6` for weekly policies. Custom policies specify `custom_start`, `custom_end`, optional `repeat_interval_seconds`, and `expiry_action`. Application is `immediate` or `next_period`; first assignments require immediate. Updates require the latest revision, with competing changes returning HTTP 409.

Normalized JSONL line or mock request:

```json
{"record_id":"nas-a:session-42:interim-2","username":"demo@example.com","session_id":"nas-a:session-42","observed_at":"2026-10-20T10:00:00Z","kind":"cumulative","upload":1000000000,"download":2000000000,"unit":"bytes"}
```

`kind` is `cumulative` or `delta`. Units: `bytes`, `KB`, `MB`, `GB`, `KiB`, `MiB`, `GiB`. Values are nonnegative integers. IDs are required; do not generate a new record ID when replaying the same source event. Each JSONL record must end with a newline.

RADIUS JSONL (`ACCOUNTING_SOURCE=radius-jsonl`):

```json
{"record_id":"nas-a:42:interim-2","User-Name":"demo@example.com","Acct-Unique-Session-Id":"nas-a:42","Acct-Session-Id":"42","Event-Timestamp":1792490400,"Acct-Input-Octets":1000,"Acct-Output-Octets":2000,"Acct-Input-Gigawords":0,"Acct-Output-Gigawords":0}
```

The exporter supplies stable IDs, trusted attribution, timestamps, correct NAS directions, and an append-only file. There is no unauthenticated accounting ingestion endpoint. Implement the `AccountingSource` protocol for an actual SQL, HTTP, or stream source once its interface is known.

Manual quota reset request:

```json
{
  "expected_period_id": 123,
  "request_id": "6179a202-8d4e-475d-b76d-7b71b796ee50",
  "reason": "Support-approved allowance reset"
}
```

Use the subscriber's `period_id` from `/api/users` or `/api/dashboard`. The response returns `reset_id`, `effective_at`, the new `period_id`, and the latest subscriber view. Replaying the same request is idempotent; reuse with different parameters or a changed current period returns 409. There must be an active interval; an expired custom interval returns 422. The reset preserves accounting records and baselines, closes the current segment, and queues a NORMAL decision atomically with its audit entry. The scheduled reset and pending assignment remain unchanged. `/api/users/{id}/history` includes reset reason, actor, timestamp, and both period references.

The browser event stream uses the same HttpOnly admin cookie as the dashboard; bearer authentication also works for streaming HTTP clients. Session validity is rechecked on notifications and at least every 15 seconds. Notifications contain no subscriber data; authorized clients receive data only through a fresh read-only, repeatable-read snapshot. Browser reconnects receive a full snapshot, so notification loss during disconnects cannot leave the interface permanently stale.
