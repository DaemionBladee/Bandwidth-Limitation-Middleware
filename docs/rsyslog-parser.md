# rsyslog accounting parser

`ACCOUNTING_SOURCE=rsyslog` connects the existing rsyslog raw JSONL capture directly to the middleware worker. No separate normalization daemon or intermediate accounting file is required. The accounting ledger, subscriber discovery, fragment state, file checkpoint, and durable enforcement jobs commit in the same PostgreSQL transaction.

The parser has automated synthetic fixtures. It has **not been verified against a real capture from your ISE deployment**; its supported formats are explicit below. Live ANC endpoint mapping/confirmation remains separate integration work.

## Enable the parser

First complete the receiver's [installation and local test](rsyslog-setup.md). Keep the capture append-only. Give the normal user or Ubuntu service account read-only access to the capture as described in [the integration step](rsyslog-setup.md#11-connect-accounting-to-the-middleware).

Configure the middleware's untracked `.env`:

```dotenv
ACCOUNTING_SOURCE=rsyslog
ACCOUNTING_FILE=/var/log/bandwidth-engine/ise-raw.jsonl
RSYSLOG_ALLOWED_SENDERS=127.0.0.1,::1
RSYSLOG_FRAGMENT_TIMEOUT_SECONDS=900
ISE_MODE=mock
```

For real collection, replace the sender list with the actual trusted sending node IPs/CIDRs, for example `10.20.30.10,10.20.30.11`. These must agree with the rsyslog receiver/network configuration. A sender outside the allowlist blocks the batch and reports an error; it cannot create subscribers or increase usage. CIDR entries are supported when deliberately configured.

From the repository directory, run a read-only preflight before enabling ingestion:

```bash
uv run --frozen python -m bandwidth.cli check-syslog --file /var/log/bandwidth-engine/ise-raw.jsonl
```

For the installed Ubuntu service account, from `/opt/bandwidth-engine`:

```bash
sudo -u bandwidth .venv/bin/python -m bandwidth.cli check-syslog --file /var/log/bandwidth-engine/ise-raw.jsonl
```

This reads the current capture and prints counts only. It does not create users, alter usage, save checkpoints, or send ISE requests. An incomplete last line or fragment group is reported with a nonzero exit; retain the data and allow the capture to complete. A file containing only the local connectivity test correctly reports zero parsed accounting records.

After successful preflight, select an enabled default quota plan and restart `make dev`, or restart both Ubuntu API/worker services. The normal ingest job now reads and parses the raw file. Keep ISE on mock until its deployment-specific live integration is verified.

## Input envelope and payloads

Every complete input line is the rsyslog receiver's JSON object. It requires a trusted `source_ip` and string `raw_message`. Other envelope metadata is retained by rsyslog; receipt time is not substituted for accounting time.

Supported `raw_message` forms:

| Format | Supported accounting body |
| --- | --- |
| RFC5424 | Full header, NILVALUE or structured-data blocks, then flat RADIUS JSON or comma-separated `key=value` attributes |
| RFC3164 | Header with optional `tag:` prefix, then RADIUS JSON/attributes with an explicit accounting event time |
| Bare message | Flat RADIUS JSON or comma-separated attributes with an explicit accounting event time |
| Cisco ISE CISE | `CISE_RADIUS_Accounting MESSAGE_ID TOTAL INDEX PAYLOAD`, including multiple fragments |
| CISE as RFC5424 APP-NAME | `CISE_RADIUS_Accounting` supplied by the app field, with message ID/count/index at the start of MSG |

For CISE, fragment indexes are **zero-based** (`0..TOTAL-1`). The accounting payload can include the conventional `YYYY-MM-DD HH:MM:SS.sss +HH:MM` ISE timestamp and message preamble before its attributes. Fragments are joined in index order without adding separators, preserving attributes split across messages. Other CISE categories are ignored and never interpreted as accounting.

This is an illustrative accounting body, not a captured Cisco log:

```text
User-Name=alice, NAS-IP-Address=192.0.2.10, Acct-Session-Id=s42, Acct-Status-Type=Interim-Update, Event-Timestamp=2026-10-20T11:59:00Z, Acct-Input-Octets=1000, Acct-Output-Octets=2000
```

Flat JSON with the same field names is also accepted. Attribute names are case-insensitive and allow punctuation-free aliases such as `UserName` and `AcctInputOctets`. CSV values support quoted commas, doubled quotes, and escaped delimiters. Conflicting duplicate fields are rejected. Whitespace-separated attributes, arbitrary nested JSON, other fragmentation schemes, and vendor-specific counter aliases require an explicit parser extension after inspecting actual samples.

## Required semantics

| Field | Interpretation |
| --- | --- |
| `User-Name` | Exact subscriber identity; nonempty and without whitespace, consistent with subscriber registration |
| `Acct-Status-Type` | Start/Stop/Interim-Update, or numeric 1/2/3; NAS-wide Accounting-On/Off (7/8) are ignored |
| `Acct-Unique-Session-Id` | Preferred globally unique session identity |
| `Acct-Session-Id` plus `NAS-IP-Address` or `NAS-Identifier` | Required alternative; the parser namespaces the session by NAS to prevent cross-device collisions |
| `Event-Timestamp` | Preferred event time, epoch seconds or an offset-aware ISO timestamp |
| CISE payload time / RFC5424 header time | Fallback if `Event-Timestamp` is absent; fragmented messages use fragment zero's header, independent of arrival order |
| `Acct-Input-Octets` | Subscriber upload, using the existing NAS-direction convention |
| `Acct-Output-Octets` | Subscriber download |
| `Acct-Input-Gigawords`, `Acct-Output-Gigawords` | Optional upper 32 bits; when supplied, both octet and gigaword fields must fit unsigned 32-bit values |
| `Acct-Session-Time` | Optional elapsed seconds; provides a stable ordering key for same-time records |

Missing octet counters become zero **only for Start events**. Interim/Stop records require both counters. Negative, fractional, boolean, overflowing, or malformed counters are rejected. Without gigaword fields, full-width octet values within the middleware's supported range are accepted.

Times must include an offset unless they are epoch values. A yearless RFC3164 header or rsyslog `received_at`/`reported_at` alone cannot supply an accounting time. The ingest layer rejects future events. Distinct samples at the same timestamp need adequate event precision or consistent session-time ordering from the source; unobserved counter resets cannot be inferred from missing measurements.

The parser creates deterministic record IDs from normalized event content, not receipt time, syslog message ID, or ISE sender IP. Replayed events therefore reach the existing duplicate-safe cumulative accounting ledger. Counter resets, late/out-of-order records, session changes, quota periods, and NORMAL/LIMITED decisions use the existing ledger/engine.

## Fragments, checkpoints, and errors

Pending fragments are stored in the rsyslog accounting checkpoint's JSONB cursor. They survive batch boundaries and worker/process restarts. A group is scoped by sender, originating host, CISE category, and message ID. Out-of-order and identical duplicate pieces are accepted; conflicting pieces/counts fail the batch. A bounded cache of completed group hashes handles recent duplicate fragment deliveries.

Resource limits are explicit: 64 pieces per group, 128 pending groups, 1 MiB per raw line/message/assembled message, 2 MiB of serialized pending fragment state, and 128 cached completed groups. Limits stop processing with a diagnostic rather than silently discarding accounting.

At end of available input, a group incomplete for `RSYSLOG_FRAGMENT_TIMEOUT_SECONDS` reports an error. It is retained. Later appended fragments can complete it and resume ingestion; processing can advance through subsequent batches to reach that data. A partial last JSONL line waits for its newline.

Malformed/unsupported accounting, unknown subscribers without an enabled default, or source errors roll back the whole batch. Its file offset, fragment state, usage changes, and jobs are not partially committed. Error messages identify a byte offset or field/format issue without copying raw messages or credentials. Ordinary connectivity messages and recognized non-accounting CISE categories advance the checkpoint as ignored lines.

The active raw file is now the accounting source and must remain append-only. Replacement/rotation/truncation requires controlled source/checkpoint reconciliation; see [retention and migration](rsyslog-setup.md#retention-and-migration). Back up the raw source together with the database. There is no automatic file-identity rebinding or fragment-discard command.

## Monitoring and local demonstration

Authenticated `/api/status` and dashboard snapshots expose parser counts under the rsyslog checkpoint's `parser` field: `lines`, `parsed`, `ignored`, and `pending_count`. `parsed` counts candidates before ledger duplicate suppression; it is not a count of newly inserted records. Fragment contents and complete checkpoint cursors are not exposed. Worker errors show failed batches and expired incomplete groups.

The local fixture can be checked without contacting ISE:

```bash
uv run --frozen python -m bandwidth.cli check-syslog --file tests/fixtures/rsyslog/synthetic-accounting.jsonl
```

Expected counts: six lines, three parsed records, one ignored connectivity message, and zero pending groups. The remaining lines are fragment deliveries, including a replay. The fixture subscriber transfers 110 combined bytes. These files are generated tests, not real ISE captures; see the [fixture description](../tests/fixtures/rsyslog/README.md).

After receiving actual accounting logs, preflight a protected redacted sample, verify field/session/time/counter semantics for the deployment, and extend unsupported variants explicitly. Parser implementation does not establish live ISE enforcement or add the MAC/IP-to-active-session mapping required by ANC.
