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

Use **Submit mock usage** in the dashboard and submit combined upload/download totaling 50 GB to request LIMITED. The worker ingests the record, commits usage and its enforcement job together, then simulates ISE confirmation. The dashboard updates automatically. Every mock state is labeled; **mock confirmation is never live network enforcement**.

## Live dashboard

The dashboard uses an authenticated Server-Sent Events connection backed by PostgreSQL LISTEN/NOTIFY. Changes committed by API processes or workers appear automatically; rolled-back changes do not emit notifications. A reconnect receives a consistent snapshot. There is no periodic browser data polling or full-page refresh. Searches, filters, pagination, open history entries, and unfinished forms stay in place. The connection indicator reports dashboard connectivity separately from ISE enforcement. Expired/revoked admin sessions close their streams.

The overview includes seven days of recorded upload/download, current allowance totals, and enforcement states. The **Subscribers** view adds search, mode filters, sorting, pagination, and a detail drawer with retained periods, usage resets, and enforcement attempts. Both light and dark themes support mobile layouts and reduced-motion preferences. Icons and fonts need no external service.

Use the globe button for **Display preferences**. Search/select an IANA timezone by city, region, or UTC offset. This browser preference controls displayed timestamps and the chart's day boundaries and persists across reloads. A policy's own searchable timezone selection controls its actual schedule and remains independent of the display preference. Ambiguous/nonexistent local reset times follow the documented DST rules below.

To grant an individual subscriber a fresh allowance, open **History** (or the row's action menu), choose **Reset usage**, provide a reason, and confirm. The current allowance segment closes at the reset timestamp; a new segment starts with the same quota until the original scheduled boundary. NORMAL is requested through the durable enforcement queue. Earlier periods, cumulative accounting baselines, and raw records are retained. Consequently the seven-day transfer chart still includes traffic from before a quota reset. Resets do not reactivate expired one-time policies or change pending policy edits. A reset is serialized with accounting, is idempotent by request ID, and rejects stale period confirmations. All resets record the acting admin and reason.

After pulling an update, stop any running `make dev`, run `git pull --ff-only`, then `make dev` again. The development script applies the new migrations before starting API/workers. Existing accounts and stored data are preserved. If using separate terminals, run `make migrate` before restarting both processes. Behind a reverse proxy, allow streaming responses and disable buffering for `/api/events`; the response includes `X-Accel-Buffering: no` and sends a heartbeat at least every 15 seconds. Each connected dashboard uses one dedicated PostgreSQL listener connection, released when it disconnects or the tab is hidden.

## Admin capabilities

### Default quota plan and automatic subscribers

In **Policy library → Default quota plan**, create a plan with its own quota, normal/limited speeds, timezone, and reset schedule. Alternatively choose **Use as default** on an existing policy. Edit or remove the default from its card. No default is enabled by a migration or by `make demo`; choose one explicitly.

An enabled default is assigned automatically when an admin/API registers a new subscriber, or when the configured accounting adapter first receives an unknown username. For real WLC users, connect a trusted RADIUS accounting export first: the app discovers identities from those records, not from a WLC connection notification or raw syslog. `ACCOUNTING_SOURCE=radius-jsonl` consumes the documented JSONL format through `ACCOUNTING_FILE`; an exporter/parser is still required for actual Cisco logs. Exporting Start events allows enrollment as soon as accounting reports a connection, with absent Start counters treated as zero. No external subscriber database sync or subscriber password provisioning is installed.

Each subscriber receives an independent allowance. First accounting discovery starts the assignment at the earliest sample timestamp in its batch, so that initial traffic is included; the currently configured default version is used for backlogged discoveries. Later records, duplicate events, new sessions, and reconnects reuse the identity and usage. Existing users without an assignment are also left unchanged. User creation, default assignment, accounting, checkpoint advancement, audit, and enforcement jobs commit together. Concurrent discoveries create only one subscriber and one initial assignment.

**Edit default plan** creates a new version for future subscribers only. Existing assignments, allowances, overrides, and pending changes are retained. To update existing subscribers, use the policy library's ordinary **Edit policy** and explicitly choose immediate or next-period application, or change an individual assignment. Removing the default stops automatic enrollment and retains the policy and all history. Disabling the default's policy pauses enrollment. Without an enabled default, unknown accounting users still stop their batch until registered; already registered users continue to ingest.

### Administrator accounts and change history

Create the first administrator with `make bootstrap`; interactive setup securely prompts for a password and confirmation. Existing accounts are never overwritten. A bootstrap password supplied through `ADMIN_BOOTSTRAP_PASSWORD` is intended for controlled setup only; remove it from persistent configuration afterward.

Signed-in administrators can create individual accounts in **Administrators → Create administrator**. New passwords must match and contain 12–512 characters. The acting administrator must confirm their own current password; five failed confirmations block account creation for five minutes. The endpoint requires a valid browser session, same-origin/CSRF checks, and password confirmation even when an automation token is present. Passwords use salted scrypt hashes and are omitted from API responses, validation errors, and audit entries. Dialog passwords clear when closed. Each administrator has full workspace access; there is no public account registration. Use HTTPS with `COOKIE_SECURE=true` outside local development.

**Change history** shows activity counts, a seven-day activity chart, category/person/day filters, and readable parameter comparisons with units, names, and localized times. Expand an event to compare **Before → After**; no raw JSON is needed. The view covers the latest 100 retained events, rather than claiming all-time statistics. Live updates preserve filters and expanded events.

- Create/edit versioned policies, positive byte quotas, normal/limited speeds, enabled status, timezone, and calendar/custom schedules. Limited speeds cannot exceed normal speeds.
- Register accounting usernames and assign policies. Disable a policy or assignment to request its NORMAL profile. Identity matching is exact and case-sensitive.
- Apply edits immediately or at each subscriber's next boundary. Expected revisions reject concurrent lost updates.
- Monitor upload, download, combined/remaining allowance, next boundary, requested mode, separately confirmed state, pending changes, errors, and enforcement attempts.
- Inspect retained quota periods and administrative change history. Assignment changes retain historical and cancelled scheduled revisions.

Authentication uses scrypt password hashes and database-backed random, hashed session tokens. Cookies are HttpOnly and SameSite=Strict; mutations also require CSRF tokens. Login attempts are throttled in PostgreSQL. `COOKIE_SECURE=false` is for local HTTP only; deploy behind HTTPS and set it to `true`. Optional `ADMIN_API_TOKEN` enables bearer authentication for automation; generate a strong secret and inject it securely. Admin APIs are under `/api`; see [API examples](docs/api.md).

## Accounting and period semantics

`ACCOUNTING_SOURCE=mock` uses a PostgreSQL inbox. `jsonl` reads normalized measurements, and `radius-jsonl` reads exported RADIUS counters from `ACCOUNTING_FILE`. The adapter interface separates source polling from accounting. No accounting service was configured in the original checkout; these are concrete local integration options, not a live feed.

Every record requires a stable, source-unique `record_id`. Identical replays are no-ops; ID reuse with different content is rejected. Source cursor and ledger updates commit atomically. A malformed record or unknown user without an enabled default blocks the batch without advancing the checkpoint; repair the exporter, configure a default, or register the username and retry. JSONL input must be append-only: partial final lines wait for completion, and rotation/truncation is rejected for operator reconciliation.

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

The browser test starts its own API and worker against the test database, signs in through Chromium, selects timezones, creates a policy/subscriber, submits accounting, and observes LIMITED plus **mock** confirmation through the event stream. It verifies unfinished forms survive updates, resets usage with retained history, exercises search/filters, edits a policy, checks light/dark/mobile layouts, persists preferences, and verifies logout revokes an open stream. It uses system Chromium (or `CHROMIUM_PATH`) and explicitly skips when unavailable. Screenshots go to ignored `artifacts/`. ISE transport tests use synthetic responses; no test claims a live appliance was changed.
