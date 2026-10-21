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
| GET | `/api/default-plan` | Current selection, configuration revision, policy revision, and specification |
| POST | `/api/default-plan` | Create/select default using `{ "spec": {…} }` or `{ "policy_id": 1 }` |
| PUT | `/api/default-plan` | Change default using specification or policy ID plus `expected_revision` |
| DELETE | `/api/default-plan?expected_revision=1` | Remove selection; retain assignments, policy, usage, and audit |
| GET | `/api/admins` | Administrator IDs/usernames; no password hashes |
| POST | `/api/admins` | Create admin; requires browser session, CSRF, and current password confirmation |
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

For a RADIUS Start event, include `"Acct-Status-Type": "Start"` (numeric `1` is also accepted). Missing octet counters are treated as zero for Start only, so a first connection can create its subscriber and allowance before any transfer. Interim/Stop records require both octet counters; missing counters are rejected rather than treated as a session reset. Export subscriber Start/Interim/Stop events, not NAS-wide Accounting-On/Off messages.

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

Default-plan creation accepts exactly one of `spec` (the policy JSON above) or `policy_id` (an existing policy). Updates additionally require the configuration's `revision` as `expected_revision`, not the policy revision. Competing edits/removals return 409; editing/removing an absent default returns 404. The configuration revision continues increasing after removal/recreation. Editing a default specification creates an immutable policy version for future subscribers; existing assignments and usage are unchanged. Ordinary `/api/policies/{id}` edits still apply the explicitly requested timing to existing assigned users and update the default's configuration revision when relevant.

New `/api/users` registrations receive the enabled default and return `default_assigned: true`. A trusted accounting source can discover unknown usernames and assign the default atomically at their first sample timestamp. Reconnects never reset allowances or replace assignments. With no enabled default, unknown accounting users are rejected as before. No public subscriber-registration or raw-syslog endpoint is added. In mock mode, authenticated `/api/mock/accounting` also accepts a new username when an enabled default is configured; the worker performs actual registration/assignment. Snapshot/SSE responses include `default_plan` and administrator IDs/usernames. Audit responses additionally include a readable `entity_label`.

Administrator creation body:

```json
{
  "username": "network.operator",
  "password": "<new 12–512 character password>",
  "confirm_password": "<same new password>",
  "current_password": "<acting administrator's password>"
}
```

The existing admin browser cookie and `X-CSRF-Token` are required; a bearer token alone cannot create accounts. Invalid current-password confirmations return 403 and are limited to five failures per administrator per five minutes (429 while blocked). Username conflicts return 409. Invalid/mismatched new credentials return 422 without echoing input. Success returns only `{ "id": …, "username": … }`. Audit includes the new username and acting admin, never password fields. There is no public bootstrap endpoint; use the CLI for first setup.
