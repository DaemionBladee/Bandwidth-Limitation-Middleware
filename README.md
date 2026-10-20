# Bandwidth Engine

Quota middleware with a FastAPI admin API, responsive dashboard, PostgreSQL accounting ledger, durable background jobs, and a deployment-gated Cisco ISE adapter. Python 3.12+, PostgreSQL 16+, [uv](https://docs.astral.sh/uv/), and Docker Compose are required for the local workflow. No framework, migrations, authentication, accounting source, or ISE configuration existed in the initial repository.

## Run locally

Run from the existing checkout (each cloud task is already isolated; no worktree is necessary):

```sh
cp .env.example .env                  # only if .env does not already exist
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/bandwidth-uv-cache}"
uv sync --frozen
docker compose up -d --wait db
uv run --frozen alembic upgrade head
uv run --frozen python -m bandwidth.cli bootstrap  # securely prompts for a 12+ character password
uv run --frozen python -m bandwidth.cli demo
```

Start both processes in separate terminals, from this repository:

```sh
.venv/bin/uvicorn bandwidth.api:app --host 127.0.0.1 --port 8000
.venv/bin/python -m bandwidth.worker
```

Or run `bash scripts/dev.sh` after initial setup to start the database, API, and worker together. The dashboard is served at the API root, port 8000. Sign in using the admin username (`admin` by default) and password you chose. `GET /health` verifies database connectivity. Stopping the script stops its API and worker; PostgreSQL remains available through Compose. `docker compose stop db` stops the database without deleting its volume.

The demo is **50 GB per calendar month, 20/10 Mbps normal, 2/1 Mbps limited**, in UTC, resetting at midnight on day 1. Units are decimal: 50 GB = 50,000,000,000 bytes; 1 Mbps = 1,000 kbps. The demo subscriber is `demo@example.com`. The demo command is repeatable and leaves existing policies, users, and credentials unchanged.

Use **Submit mock usage** in the dashboard and submit combined upload/download totaling 50 GB to request LIMITED. The worker ingests the record, commits usage and its enforcement job together, then simulates ISE confirmation. Refresh to see the outcome. Every mock state is labeled; **mock confirmation is never live network enforcement**.

## Admin capabilities

- Create/edit versioned policies, positive byte quotas, normal/limited speeds, enabled status, timezone, and calendar/custom schedules. Limited speeds cannot exceed normal speeds.
- Register accounting usernames and assign policies. Disable a policy or assignment to request its NORMAL profile. Identity matching is exact and case-sensitive.
- Apply edits immediately or at each subscriber's next boundary. Expected revisions reject concurrent lost updates.
- Monitor upload, download, combined/remaining allowance, next boundary, requested mode, separately confirmed state, pending changes, errors, and enforcement attempts.
- Inspect retained quota periods and administrative change history. Assignment changes retain historical and cancelled scheduled revisions.

Authentication uses scrypt password hashes and database-backed random, hashed session tokens. Cookies are HttpOnly and SameSite=Strict; mutations also require CSRF tokens. Login attempts are throttled in PostgreSQL. `COOKIE_SECURE=false` is for local HTTP only; deploy behind HTTPS and set it to `true`. Optional `ADMIN_API_TOKEN` enables bearer authentication for automation; generate a strong secret and inject it securely. Admin APIs are under `/api`; see [API examples](docs/api.md).

## Accounting and period semantics

`ACCOUNTING_SOURCE=mock` uses a PostgreSQL inbox. `jsonl` reads normalized measurements, and `radius-jsonl` reads exported RADIUS counters from `ACCOUNTING_FILE`. The adapter interface separates source polling from accounting. No accounting service was configured in the original checkout; these are concrete local integration options, not a live feed.

Every record requires a stable, source-unique `record_id`. Identical replays are no-ops; ID reuse with different content is rejected. Source cursor and ledger updates commit atomically. A malformed record or unknown user blocks the batch without advancing the checkpoint; repair the exporter or register the username and retry. JSONL input must be append-only: partial final lines wait for completion, and rotation/truncation is rejected for operator reconciliation.

Cumulative records are ordered by timestamp, then record ID within source/user/session. A delayed sample corrects the subsequent sample's delta and affected totals, including completed periods. A counter decrease starts a new counter segment. A new session ID starts an independent baseline. Exporters must use unique session IDs across NAS/reboots (prefer `Acct-Unique-Session-Id`) and report resets, including zero samples where possible: an unobserved reset whose new counter exceeds the previous counter cannot be inferred from counters alone.

**Attribution is by the sample's event timestamp**, with periods half-open `[start, end)`. A cumulative sample represents the delta at its sample end; it is not interpolated across a boundary because the counters do not reveal when the bytes were transferred. For precise split-period attribution, export delta measurements at the required granularity. Future timestamps are rejected. Upload plus download is the quota default. RADIUS NAS input maps to subscriber upload, NAS output to download, including 32-bit gigaword counters. The app reads exports; it is not a RADIUS server.

Daily, weekly (Monday = 0), and monthly resets use IANA timezones and local wall time. An absent monthly day clamps to the month's final day; the next month retains the configured day. Ambiguous DST times choose the first occurrence; nonexistent times move forward by the DST gap. Calendar periods can be 23 or 25 hours.

Custom intervals require offset-aware start/end timestamps. Optional repeats use elapsed UTC seconds, cannot overlap, and are anchored to the original start. One-time intervals and repeat gaps require an explicit `NORMAL` or `LIMITED` expiry action. Before the first start the requested mode is NORMAL. Expiry is an action boundary, not a new allowance; a one-time interval never silently repeats.

An immediate edit creates a new policy view and recalculates existing ledger usage within its current window. Changing the schedule can change that window; old, potentially overlapping views are retained and must not be summed. A next-period edit preserves the current view until its boundary, then starts a fresh allowance; an overlapping new calendar window is clipped to that activation boundary. A newly scheduled change replaces prior pending changes for that subscriber, retaining them as cancelled history. Completed records are retained and may receive late accounting corrections.

## Workers and enforcement

`python -m bandwidth.worker` schedules four persistent jobs: accounting ingestion, quota evaluation, rollover, and reconciliation. Decisions use NORMAL below quota, LIMITED exactly at or above quota. The same transaction persists each new desired generation and its unique enforcement job. PostgreSQL row locks serialize subscriber updates; `FOR UPDATE SKIP LOCKED` plus recoverable leases distribute jobs between workers.

Enforcement re-evaluates the current policy and period before I/O. Obsolete generations become stale. The worker holds the subscriber lock during bounded ISE requests, so an older job cannot finish after a newer decision has committed. Requests default to 5-second timeouts. Each job retries at most five times with exponential backoff; reconciliation starts another bounded burst after a five-minute cooldown and probes confirmed states for drift. Manual **Reconcile** queues a new generation immediately. Attempts and sanitized errors are retained. A crash between remote I/O and commit is reconciled by readback; configured operations must be repeatable.

The queue is durable and at-least-once. Completed jobs are retained. Configure backups, monitoring, retention, and a least-privilege database role before a shared deployment; no cleanup deletes historic quota usage automatically.

## Cisco ISE integration

`ISE_MODE=mock` is the default. No live appliance version, enabled APIs, authorization rules, profiles, or credentials were supplied. **Live ISE enforcement is blocked until those are inspected.** There is no guessed Cisco endpoint, bandwidth attribute, or WLC QoS command. See [the ISE deployment contract](docs/ise.md) for inspection, mapping, and session confirmation requirements. Unsupported API/authentication or response formats require an adapter extension after discovery.

The live adapter requires HTTPS with certificate verification, verified deployment metadata, exact version readback, existing profile mappings for each requested speed pair, and active-session confirmation. A successful update/CoA HTTP response alone never confirms applied state. Credentials belong in environment variables or a secret store; do not commit them or put them in the contract.

## Verification

```sh
make test
make check
```

Tests create/use a separate `bandwidth_test` database and truncate only databases with the `_test` suffix. The local database user can create it. Override `TEST_DATABASE_URL` for a separate test database, still ending in `_test`. Never use shared application data. PostgreSQL, rather than SQLite, exercises locks and concurrency. Migrations upgrade an empty database and `alembic check` compares schema to models.

The browser test starts its own API and worker against the test database, signs in through Chromium, creates a policy/subscriber, assigns it, submits accounting, observes LIMITED plus **mock** confirmation, views history, edits the policy, and checks mobile layout. It uses system Chromium (or `CHROMIUM_PATH`) and explicitly skips when unavailable. Screenshots go to ignored `artifacts/`. ISE transport tests use synthetic responses; no test claims a live appliance was changed.
