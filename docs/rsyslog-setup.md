# rsyslog installation and middleware integration

Use this guide for **CachyOS collection tests** and **Ubuntu Server deployment** on `codex/updated-middleware-workflow`. You can install and test the receiver before installing the middleware database or configuring any ISE credentials.

The receiver captures raw logs today. The branch does **not** yet contain a Cisco syslog accounting parser: real log samples are needed to implement it. The middleware consumes normalized or RADIUS JSONL accounting records. Raw capture and quota ingestion are separate stages.

```mermaid
flowchart LR
    I[ISE / WLC accounting logs] --> R[rsyslog receiver]
    R --> F[Raw JSONL capture]
    F -. Parser still required .-> N[Supported accounting JSONL]
    N --> W[Middleware worker]
    W --> D[(Usage, policies, jobs)]
    W -. Verified ANC integration .-> A[Cisco ISE]
```

## Contents

1. [Choose the host, addresses, and port](#1-choose-the-host-addresses-and-port)
2. [Install rsyslog](#2-install-rsyslog)
3. [Get the receiver configuration](#3-get-the-receiver-configuration)
4. [Install the configuration for your OS](#4-install-the-configuration-for-your-os)
5. [Include, validate, and start](#5-include-validate-and-start)
6. [Test collection locally](#6-test-collection-locally)
7. [Choose TCP or UDP and change the port](#7-choose-tcp-or-udp-and-change-the-port)
8. [Accept the real sending node](#8-accept-the-real-sending-node)
9. [Configure firewall and ISE forwarding](#9-configure-firewall-and-ise-forwarding)
10. [Capture the accounting samples](#10-capture-the-accounting-samples)
11. [Connect normalized accounting to the middleware](#11-connect-normalized-accounting-to-the-middleware)
12. [Complete ANC enforcement](#12-complete-anc-enforcement)

References: [troubleshooting](#troubleshooting), [retention and migration](#retention-and-migration), [verification](#verification).

## 1. Choose the host, addresses, and port

Run the operating-system commands on the machine that will **receive logs**. Your current CachyOS workstation can receive test logs; the planned production receiver is Ubuntu Server. Each host needs its own receiver configuration and log directory. Installing rsyslog on one does not configure the other.

| Setting | Initial local test | Example network collection |
| --- | --- | --- |
| Receiver OS | CachyOS or Ubuntu | Ubuntu Server, or CachyOS for a temporary test |
| Listening address | `127.0.0.1` | Receiver's actual LAN address, for example `10.20.30.20` |
| Trusted sender | `127.0.0.1` | Actual sending ISE node, for example `10.20.30.10` |
| Transport/port | TCP `5514` | Same transport and port on receiver, sender, and firewall |
| Raw output | `/var/log/bandwidth-engine/ise-raw.jsonl` | Same path on the selected receiver |

The `10.20.30.x` addresses are examples; replace them with your own. You can finish the loopback test while the LAN and ISE addresses are undecided. Find assigned IPv4 addresses with:

```bash
ip -br -4 address
```

Choose the appropriate Ethernet/Wi-Fi address for network collection. Use a stable address or DHCP reservation for a lasting ISE logging target.

Port `5514` is the incoming syslog receiver in this example. ISE's HTTPS **ANC API port `9060`** is a separate outbound connection from the middleware. Existing WLC-to-ISE RADIUS accounting also has its own settings; changing the syslog listener does not change those.

## 2. Install rsyslog

### CachyOS

If rsyslog is already installed, check it and continue to step 3:

```bash
rsyslogd -v
command -v logger
```

For a fresh installation, first check whether your configured CachyOS/Arch repositories offer the package:

```bash
pacman -Si rsyslog
```

If it is available there, install it as part of a full package update:

```bash
sudo pacman -Syu --needed rsyslog nano
```

If the package is unavailable in those repositories, the alternative is the [rsyslog AUR package](https://aur.archlinux.org/packages/rsyslog). Build as your normal user after reviewing its `PKGBUILD` and any install files:

```bash
sudo pacman -Syu --needed base-devel git nano
mkdir -p ~/builds
cd ~/builds
git clone https://aur.archlinux.org/rsyslog.git
cd rsyslog
less PKGBUILD
makepkg -si
```

Use the package source appropriate to your system; repository availability and AUR build requirements can change. If the build directory already exists, review and update that checkout instead of cloning over it. `makepkg` runs as your normal user and requests installation privileges when needed.

### Ubuntu Server 24.04 LTS

```bash
sudo apt update
sudo apt install -y rsyslog nano
rsyslogd -v
command -v logger
getent passwd syslog
getent group syslog
```

The Ubuntu package provides the `syslog` user/group used by the supplied receiver. CachyOS may run rsyslog as root and have no `syslog` account; the next steps handle that difference.

## 3. Get the receiver configuration

If you already have this repository, open a terminal in its root directory and use the current workflow branch. Run Git as the checkout's owner; for a root-owned Ubuntu deployment, use the administrative Git workflow in the [Ubuntu guide](ubuntu-server.md#update-the-server).

```bash
git fetch origin
git switch codex/updated-middleware-workflow
git pull --ff-only
```

For a new normal-user checkout:

```bash
cd ~
git clone --branch codex/updated-middleware-workflow https://github.com/DaemionBladee/Bandwidth-Limitation-Middleware.git
cd Bandwidth-Limitation-Middleware
```

The repository is private: use your authorized credential helper, GitHub token, or SSH authentication. Keep credentials out of clone URLs and configuration files.

All relative paths below start at the repository root. The common receiver source is [deploy/rsyslog/30-bandwidth-engine.conf](../deploy/rsyslog/30-bandwidth-engine.conf). It includes the JSON template, trusted-sender check, TCP listener, and file permissions. No Python environment or database is needed to copy and test this file.

If you already have a working receiver, retain its custom addresses/port and follow the relevant sections below. Before replacing an existing configuration, back it up **outside** the included rsyslog configuration directory.

## 4. Install the configuration for your OS

### CachyOS with the default root service

First inspect the service identity and create the directory that may be missing on CachyOS:

```bash
systemctl show rsyslog.service -p User -p Group
sudo install -d -m 0755 /etc/rsyslog.d
sudo grep -RniE 'PrivDropTo(User|Group)' /etc/rsyslog.conf /etc/rsyslog.d
```

Empty systemd `User`/`Group` values mean systemd starts the service as root. Also check the last command for rsyslog's own active privilege-drop settings; commented examples are inactive, and no matches is normal. The following commands apply when the effective rsyslog identity is root, as in the successful CachyOS local test:

```bash
sudo install -d -o root -g root -m 0750 /var/log/bandwidth-engine
sudo install -m 0644 deploy/rsyslog/30-bandwidth-engine.conf /etc/rsyslog.d/30-bandwidth-engine.conf
sudo sed -i \
  -e 's/fileOwner="syslog"/fileOwner="root"/g' \
  -e 's/fileGroup="syslog"/fileGroup="root"/g' \
  /etc/rsyslog.d/30-bandwidth-engine.conf
sudo grep -iE 'fileOwner|fileGroup' /etc/rsyslog.d/30-bandwidth-engine.conf
```

The final command must show `fileOwner="root"` and `fileGroup="root"`. This avoids `ID for user 'syslog' could not be found`. If your service uses another account, use that account and its permitted group for the directory and file settings instead. The file-ownership parameters do not change the daemon's service identity.

### Ubuntu Server

```bash
sudo install -d -m 0755 /etc/rsyslog.d
sudo install -d -o syslog -g syslog -m 0750 /var/log/bandwidth-engine
sudo install -m 0644 deploy/rsyslog/30-bandwidth-engine.conf /etc/rsyslog.d/30-bandwidth-engine.conf
sudo grep -iE 'fileOwner|fileGroup' /etc/rsyslog.d/30-bandwidth-engine.conf
```

Keep `fileOwner="syslog"` and `fileGroup="syslog"` on the standard Ubuntu setup. Inspect custom service/privilege-drop settings if you have changed its identity.

## 5. Include, validate, and start

Open the main configuration:

```bash
sudo nano /etc/rsyslog.conf
```

Ensure it loads `/etc/rsyslog.d/*.conf` once. An existing `$IncludeConfig /etc/rsyslog.d/*.conf` is sufficient. If no include loads that directory, add:

```rsyslog
include(file="/etc/rsyslog.d/*.conf" mode="optional")
```

Save Nano with **Ctrl+O**, **Enter**, then **Ctrl+X**. Keep one copy of the Bandwidth receiver; duplicate files such as `30-cisco-ise.conf` and `30-bandwidth-engine.conf` containing the same definitions will conflict. Move obsolete copies/backups outside the included directory after reviewing them.

The receiver loads `imtcp`. If your main configuration already loads it, retain one module load and remove the duplicate from the receiver file. Review the shared template's global `maxMessageSize="128k"` against any existing message-size setting.

Validate before restarting:

```bash
sudo rsyslogd -N1 && sudo systemctl restart rsyslog.service
sudo systemctl enable rsyslog.service
sudo systemctl status rsyslog.service --no-pager
```

`active (running)` establishes process state. `enabled` only means it is enabled for startup; it does not prove the process is running. If validation fails, resolve its error before starting/restarting the service.

Newer rsyslog versions, including the reported CachyOS 8.2610 build, can warn that `streamdriver.mode="0"` uses plain TCP. This is distinct from the missing-account error. The supplied listener is plain TCP for the initial loopback test. TLS needs the appropriate driver, certificates, peer validation, and matching ISE settings; changing global secure defaults alone does not configure those.

## 6. Test collection locally

The unchanged listener and sender filter both use `127.0.0.1`. Check TCP port `5514` and send a message:

```bash
sudo ss -lntp 'sport = :5514'
logger --tcp --server 127.0.0.1 --port 5514 --tag bandwidth-test 'Receiver connectivity test'
sudo tail -n 1 /var/log/bandwidth-engine/ise-raw.jsonl
```

Allow a moment for the receiver to write the record. Expected fields include:

```json
{"received_at":"2026-11-10T09:00:00+06:30","reported_at":"2026-11-10T09:00:00+06:30","source_ip":"127.0.0.1","hostname":"receiver-host","raw_message":"... bandwidth-test ... Receiver connectivity test"}
```

This demonstrates local transport, JSON encoding, and file output. It does not demonstrate ISE accounting, subscriber creation, quota usage, or ANC enforcement. Keep `ACCOUNTING_SOURCE=mock` and `ISE_MODE=mock` while collecting initial samples.

## 7. Choose TCP or UDP and change the port

Edit the installed receiver:

```bash
sudo nano /etc/rsyslog.d/30-bandwidth-engine.conf
```

The TCP listener is:

```rsyslog
input(type="imtcp" address="127.0.0.1" port="5514" ruleset="bandwidth_raw_capture")
```

Change `port="5514"` to your chosen available port, for example `port="1514"`. Change the same port in your test command, firewall rule, and ISE logging target. Validate/restart after saving. For that example:

```bash
sudo rsyslogd -N1 && sudo systemctl restart rsyslog.service
sudo ss -lntp 'sport = :1514'
logger --tcp --server 127.0.0.1 --port 1514 --tag bandwidth-test 'Alternate TCP port test'
sudo tail -n 1 /var/log/bandwidth-engine/ise-raw.jsonl
```

If the actual ISE target uses **UDP**, replace the Bandwidth module/input with:

```rsyslog
module(load="imudp")
input(type="imudp" address="127.0.0.1" port="5514" ruleset="bandwidth_raw_capture")
```

Keep the template, ruleset, sender filter, and ownership settings. Keep one load of each module used by the host. Validate/restart and test UDP instead:

```bash
sudo rsyslogd -N1 && sudo systemctl restart rsyslog.service
sudo ss -lnup 'sport = :5514'
logger --udp --server 127.0.0.1 --port 5514 --tag bandwidth-test 'UDP receiver test'
sudo tail -n 1 /var/log/bandwidth-engine/ise-raw.jsonl
```

Choose the transport supported by your ISE logging target. UDP has no delivery acknowledgement; lost accounting messages can affect usage totals. The template does not configure TLS. Where secure syslog is required, configure and verify TLS on both ends, including the actual appliance's certificate/peer requirements, before carrying production traffic.

## 8. Accept the real sending node

Once addresses are assigned, replace the two loopback values with their **different roles**. For the example receiver `10.20.30.20` and sender `10.20.30.10`, the ruleset check becomes:

```rsyslog
if $fromhost-ip != "10.20.30.10" then stop
```

The TCP input becomes:

```rsyslog
input(type="imtcp" address="10.20.30.20" port="5514" ruleset="bandwidth_raw_capture")
```

For UDP use `imudp` instead. The `address` must belong to the receiver; use the actual source IP of the sending ISE node in the filter. If an approved relay/NAT is involved, account for the source address seen by the receiver and establish trusted origin attribution through that relay.

For multiple legitimate senders, explicitly allow each one:

```rsyslog
if ($fromhost-ip != "10.20.30.10" and $fromhost-ip != "10.20.30.11") then stop
```

Validate/restart again and inspect the listener. After changing to a network address and ISE-only filter, the earlier localhost test will no longer prove remote collection: the listener/filter now target the real sender. `address="0.0.0.0"` would listen on all IPv4 interfaces, while the sender filter still applies; a specific LAN binding is easier to verify for this deployment.

## 9. Configure firewall and ISE forwarding

Use the firewall already managed on the receiver. The following rules use the example addresses and TCP port `5514`; replace them with your values and use `udp` if selected. Add every required sending node. Keep existing SSH and dashboard rules.

### UFW, when already in use

```bash
sudo ufw status
sudo ufw allow from 10.20.30.10 to 10.20.30.20 port 5514 proto tcp
```

### firewalld, when already in use

Identify the zone for the receiver's network interface:

```bash
sudo firewall-cmd --get-active-zones
```

If that zone is `public`, add the rule for the running and persistent configuration:

```bash
sudo firewall-cmd --zone=public --add-rich-rule='rule family="ipv4" source address="10.20.30.10" port port="5514" protocol="tcp" accept'
sudo firewall-cmd --permanent --zone=public --add-rich-rule='rule family="ipv4" source address="10.20.30.10" port port="5514" protocol="tcp" accept'
```

Use the actual active zone if different. Installing/enabling another firewall is not required by this guide. Any intervening network ACL also needs to permit the selected connection.

In the actual ISE version's **remote logging target** settings, configure:

| Setting | Example |
| --- | --- |
| Destination | Receiver LAN IP `10.20.30.20` |
| Transport | TCP, or the supported protocol you selected on the receiver |
| Destination port | `5514`, or your selected port |
| Logging categories | Relevant RADIUS accounting events containing session/counter data |

The WLC must already generate accounting Start, Interim-Update, and Stop information toward the appropriate accounting infrastructure. Assign the remote target to the appropriate accounting categories on the sending ISE nodes. Menu/category names and available transports depend on the actual ISE version; inspect that deployment's documentation instead of copying settings from another version. Authentication-only logs are insufficient for byte quotas.

Generate an authorized test subscriber session and inspect new captures:

```bash
sudo tail -f /var/log/bandwidth-engine/ise-raw.jsonl
```

Use Ctrl+C to stop following the file. A real record's `source_ip` should match an allowed sender. If nothing arrives, see the troubleshooting table before changing the accounting adapter.

## 10. Capture the accounting samples

Capture representative Start, Interim-Update, and Stop events. Preserve the relationships between messages when redacting subscriber identifiers. The parser needs the actual field names and semantics for:

- Subscriber username and endpoint MAC/IP, plus NAS identity.
- Stable session/event IDs, timestamps/timezone, and any message sequence/fragment fields.
- Upload/download octets, gigawords or other counter-width information, and accounting status.
- Repeated/interim records and examples of fragmentation or resets when available.

`received_at` is receiver time; `reported_at` comes from rsyslog's parsed header. The future parser must establish the correct accounting event time from the actual message. It must handle any Cisco message reassembly, trusted source checks, stable IDs, and NAS counter direction before emitting supported measurements.

The raw file can be read by root and the configured logging owner/group. Provide a future parser identity with explicit read access to that file/directory; the middleware only needs read access to the normalized output. Log collection does not require ISE ERS credentials or changes to website Admin/User accounts.

## 11. Connect normalized accounting to the middleware

**This section applies after a verified parser/exporter has been implemented and installed.** No Cisco syslog normalizer or its service unit is shipped in this branch. Keep testing the dashboard with mock accounting until then. See [middleware installation for CachyOS](cachyos.md) or [Ubuntu services](ubuntu-server.md) separately.

The future converter should append RADIUS JSONL records like this illustrative measurement, one complete JSON object per newline:

```json
{"record_id":"nas-a:session-42:interim-2","User-Name":"subscriber@example.com","Acct-Unique-Session-Id":"nas-a:session-42","Acct-Session-Id":"42","Event-Timestamp":1792490400,"Acct-Status-Type":"Interim-Update","Acct-Input-Octets":1000,"Acct-Output-Octets":2000,"Acct-Input-Gigawords":0,"Acct-Output-Gigawords":0}
```

This is an output schema example, not a claim that Cisco emits this raw format. For the current adapter, NAS input maps to subscriber upload and NAS output to download. Start events may omit counters, which become zero; Interim/Stop records require both octet counters. Exporters must reuse stable IDs for replays and supply unique session identities across NAS/reboots. See [all accounting formats and semantics](api.md).

Choose a persistent normalized output such as `/var/lib/bandwidth-engine/accounting/radius.jsonl`. For the Ubuntu service account, create its directory after installing the middleware:

```bash
sudo install -d -o bandwidth -g bandwidth -m 0750 /var/lib/bandwidth-engine/accounting
```

Configure the actual normalizer to append there and give `bandwidth` read access to its output. For CachyOS `make dev`, grant the normal user running the middleware access to the chosen normalized path instead. Preserve an existing file's contents. Once output exists, check Ubuntu service access:

```bash
sudo -u bandwidth test -r /var/lib/bandwidth-engine/accounting/radius.jsonl
```

Set the middleware's own `.env`:

```dotenv
ACCOUNTING_SOURCE=radius-jsonl
ACCOUNTING_FILE=/var/lib/bandwidth-engine/accounting/radius.jsonl
ISE_MODE=mock
```

Use an actual path readable by the API/worker. **Do not set `ACCOUNTING_FILE` to the raw `ise-raw.jsonl` capture.** The raw JSON envelope is a different schema. A normalized-source converter can alternatively use `ACCOUNTING_SOURCE=jsonl` with the documented normalized measurement format.

On Ubuntu, restart both processes to reload their settings:

```bash
sudo systemctl restart bandwidth-api.service bandwidth-worker.service
```

On CachyOS, stop the existing `make dev` session and run `make dev` again. Select an enabled **default quota plan** in the dashboard before discovering unknown usernames. Existing subscribers retain their assignments and usage; reconnects do not grant a new allowance.

Verify accounting checkpoint progress, usage increases from new records, duplicate-event handling, automatic first registration, and worker errors in the dashboard. A captured file alone does not show that ingestion has succeeded. With `ISE_MODE=mock`, resulting enforcement confirmations remain simulations.

## 12. Complete ANC enforcement

The operator-selected operation is:

```http
PUT https://<ISE-IP>:9060/ers/config/ancendpoint/apply
Accept: application/json
Content-Type: application/json
Authorization: Basic <constructed from server-side ERS credentials>
```

Use the [ISE deployment contract](ise.md) to configure verified connection values, the actual request body, speed-to-policy mapping, required reauthorization, and active-session confirmation. Credentials belong in the server's `.env` or secret store. The client constructs Basic authentication; the receiver file contains no ERS password.

ANC applies a policy to an endpoint. ISE authorization/WLC settings must make that policy produce the requested speed profile. The current adapter still needs verified MAC/IP identity bindings and support for the deployment's session model; its existing username binding is not a MAC/IP mapping. Establish the NORMAL reset action explicitly: applying a NORMAL policy or clearing the limited policy, according to the verified deployment.

Only then select live mode and restart both application processes. An ANC HTTP success or selected policy alone is not proof of applied bandwidth. Confirm the expected profile on the relevant active endpoint/session(s). The middleware does not configure WLC QoS directly.

## Troubleshooting

| Symptom | Check or correction |
| --- | --- |
| Nano: `No such file or directory` | Keep the editor buffer, create `/etc/rsyslog.d` in another terminal, then save. Ensure the main file includes the directory. |
| `fileOwner`: user `syslog` not found | On a default root-running CachyOS service, use `root:root` for the receiver file settings and directory. Ubuntu uses its installed `syslog` identity. Inspect custom privilege dropping. |
| Same ownership error after editing | Inspect the installed file named in the error. Another receiver file may still contain the old owner; use the ownership search in the diagnostic commands below. |
| Module/template/ruleset already defined | Keep one include and one receiver definition. Remove duplicate module loads. Store backups outside included paths. |
| Service is enabled but inactive/failed | Validate, then inspect `journalctl`; enabling only sets startup behavior. |
| `Address already in use` | Inspect `ss` for the chosen port, check duplicate listeners, or choose an available port and update sender/firewall together. |
| Plain TCP warning on rsyslog 8.2610 | Inspect the validation exit result and separate warnings from fatal errors. TLS requires driver/certificate/peer configuration on both ends. |
| Local test works, ISE sends nothing | Check LAN binding, actual sending node IP/filter, matching protocol/port, ISE category/target assignment, and host/network firewall. |
| Raw file grows, dashboard has no real usage | Accounting parser/exporter is still required. Check the normalized path/format and worker only after that stage exists. |
| Normalized accounting user is unknown | Register it or configure an enabled default quota plan. The batch/checkpoint remains blocked on errors. |
| Future accounting timestamp error | Verify event-time parsing, timezone, and clock synchronization; do not replace event time with ingestion time. |
| Accounting file rotated/truncated | Follow the checkpoint rules below; preserve data and stop/reconcile the ingestion source before resuming. |

Useful diagnostics:

```bash
sudo rsyslogd -N1
sudo systemctl status rsyslog.service --no-pager
sudo journalctl -u rsyslog.service -n 50 --no-pager
sudo grep -RniE 'file(Owner|Group).*syslog' /etc/rsyslog.d
sudo ss -lntp 'sport = :5514'
sudo ss -lnup 'sport = :5514'
```

## Retention and migration

Manage raw syslog retention/rotation separately from normalized accounting. Preserve the captured messages needed for parser development and later reconciliation. Use the host's approved rotation procedure and verify rsyslog reopens its raw output.

The current accounting adapter requires the **normalized file to remain append-only** and stores its device/inode identity plus byte offset in PostgreSQL. Truncation or replacement is rejected. Moving/copying that file to a new host/filesystem can change its identity even when its bytes are identical. Restoring the database checkpoint and copying the full file alone is therefore not enough to guarantee ingestion resumes.

For a migration, stop ingestion and take matched database/file backups. Keep the full processed prefix and stable record IDs. Resume only after an operator has reconciled the restored source identity/cursor through a controlled migration procedure or an adapter extension. There is no checkpoint-rebinding CLI in this branch. Do not clear checkpoints or delete accounting history to bypass the error.

Keep development and production accounting separate. Only the intended server should process the live feed and use live ISE credentials.

## Verification

The documented receiver configurations were exercised in an isolated Ubuntu 24.04 container: syslog-owned TCP on `5514`, root-owned TCP on `5514`, root-owned TCP on the alternate port `1514`, and root-owned UDP on `5514`. Each passed configuration validation, real `logger` delivery with synthetic messages, JSON capture, rejection of an untrusted sender, and owner/group/mode checks. Earlier Ubuntu capture checks also exercised long messages.

The reported CachyOS 8.2610 local TCP test succeeded after changing ownership to root. The root-mode container checks use Ubuntu's rsyslog build; CachyOS package installation and its complete host environment were not reproduced in the cloud workspace.

No actual ISE accounting capture or live ANC enforcement has been verified. Completion of the parser, endpoint mapping, and live contract depends on real deployment samples and capabilities. For application regression checks, see the [repository verification instructions](../README.md#verification).
