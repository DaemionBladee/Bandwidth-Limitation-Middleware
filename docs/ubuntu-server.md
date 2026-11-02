# Deploy on Ubuntu Server

This workflow uses **CachyOS for development** and a dedicated **Ubuntu Server 24.04 LTS** host for the deployed middleware. Ubuntu runs PostgreSQL 16 in Docker, the Python API and worker as separate systemd services, and Nginx for HTTPS and dashboard event streaming. The provided files are deployment examples; they do not install or configure Cisco ISE/WLC. Ubuntu itself has not been tested directly in this workspace.

```mermaid
flowchart LR
    Dev[CachyOS: edit and test with mock adapters] --> Git[GitHub workflow branch]
    Git --> API[Ubuntu: API and dashboard]
    Browser[Administrator browser] --> TLS[Nginx HTTPS]
    TLS --> API
    API --> DB[(PostgreSQL)]
    Worker[Ubuntu: background worker] --> DB
    Export[Trusted accounting exporter] --> Worker
    Worker --> ISE[Verified Cisco ISE adapter]
```

Only the server should consume the live accounting source and use the live ISE credentials. A development code update does not replace the server's database or configuration. For workstation installation, see the [CachyOS guide](cachyos.md).

## Prepare the host

Use a fresh dedicated Ubuntu 24.04 LTS host, an administrative SSH login, and a DNS name pointing at the server. The examples use `/opt/bandwidth-engine`, system account `bandwidth`, and `bandwidth.example.com`; replace the hostname with your own. Permit administrator access over HTTPS and allow SSH through your existing firewall. PostgreSQL and Uvicorn stay bound to loopback. The worker needs outbound access to the actual accounting/ISE services configured for your deployment.

```bash
sudo apt update
sudo apt install -y git make python3.12 python3.12-venv pipx docker.io docker-compose-v2 nginx certbot ca-certificates
sudo systemctl enable --now docker
pipx install uv
docker compose version
```

The package `docker-compose-v2` provides `docker compose`. Use the package source supported by your host if it is not available. This guide uses `sudo docker` for administrative database operations; the application account does not need access to the Docker socket. Node.js and Chromium are only needed for development checks, which you can run on CachyOS.

## Install code and runtime

Create the service identity and grant your administrative deployment login read access to its group:

```bash
sudo useradd --system --user-group --home-dir /var/lib/bandwidth-engine --create-home --shell /usr/sbin/nologin bandwidth
sudo usermod -aG bandwidth "$USER"
```

Open a new SSH login so the group membership takes effect. This lets your deployment login enter the checkout after it is made root-owned; code changes still require `sudo`. Then prepare the checkout:

```bash
sudo install -d -o "$USER" -g bandwidth -m 0750 /opt/bandwidth-engine
git clone --branch codex/updated-middleware-workflow https://github.com/DaemionBladee/Bandwidth-Limitation-Middleware.git /opt/bandwidth-engine
cd /opt/bandwidth-engine
"$HOME/.local/bin/uv" sync --frozen --no-dev --python /usr/bin/python3.12 --link-mode copy
cp -n .env.example .env
chmod 600 .env
```

Use your deployment login's authorized GitHub authentication for the private repository. Do not place GitHub tokens in clone URLs or copy them to the service account. Building the environment at its final path keeps executable paths valid. Using Ubuntu's system Python avoids a service dependency on files in an administrator's home directory. Copying dependencies prevents the later ownership/permission changes from affecting hard-linked files in uv's cache.

Edit the local `.env` before starting services:

| Setting | Server value |
| --- | --- |
| `POSTGRES_PASSWORD` | A new strong URL-safe password for this dedicated database |
| `DATABASE_URL` | `postgresql+psycopg://bandwidth:YOUR_DATABASE_PASSWORD@127.0.0.1:5432/bandwidth`; use the same password |
| `COOKIE_SECURE` | `true`; access the dashboard through HTTPS |
| `ADMIN_BOOTSTRAP_PASSWORD` | Leave empty to use the secure interactive prompt |
| `ACCOUNTING_SOURCE`, `ACCOUNTING_FILE` | Initially `mock` and empty; change to the inspected exporter configuration when ready |
| `ISE_MODE` | Initially `mock`; change only after completing the verified live integration |
| `ISE_*` connection/contract settings | Server-specific values and readable absolute paths; never commit credentials |

Hexadecimal database passwords avoid URL escaping issues. This sample Compose deployment uses the database owner within an isolated PostgreSQL instance; a shared/external database needs a database administrator to configure suitable application and migration privileges. Changing `POSTGRES_PASSWORD` after a volume is initialized does not rotate its existing database password.

Keep the accounting export under `/var/lib/bandwidth-engine`, and use paths under `/etc/bandwidth-engine` for a reviewed ISE contract or private CA bundle. Grant the service account read access to the configured files. The service hardening hides home directories and makes the application code read-only, so do not reference files under `/home`. A separate trusted exporter must append to the accounting file without truncating or replacing its processed prefix.

Make the deployed code read-only to the service account and protect its configuration:

```bash
sudo chown -R root:bandwidth /opt/bandwidth-engine
sudo chmod -R u=rwX,g=rX,o= /opt/bandwidth-engine
sudo chmod 640 /opt/bandwidth-engine/.env
```

Both processes read `.env` from their working directory using the application's existing settings loader. No secrets are embedded in the unit files. The dedicated service identity cannot modify the code or `.env`.

## Initialize or restore the database

From `/opt/bandwidth-engine`, start PostgreSQL using both Compose files:

```bash
sudo docker compose -f compose.yaml -f deploy/compose.ubuntu.yaml up -d --wait db
```

The override adds `restart: unless-stopped` for host/container recovery. The base file retains PostgreSQL's health check, loopback port, and persistent named volume.

For a **new deployment**:

```bash
sudo -u bandwidth .venv/bin/alembic upgrade head
sudo -u bandwidth .venv/bin/python -m bandwidth.cli bootstrap
```

The bootstrap command prompts securely for a new administrator password and confirmation. `make dev` is for a development session and is not the server process supervisor. Do not populate a real deployment with demo records unless you deliberately want them.

For a **migration of existing data**, first take a consistent database and accounting-file backup as described in [the backup procedure](cachyos.md#back-up-on-fedora-before-replacing-the-os). Restore into the fresh Ubuntu database **before** running migrations or starting either application service:

```bash
backup_dir="/path/to/protected-backup"
sudo docker compose -f compose.yaml -f deploy/compose.ubuntu.yaml exec -T db pg_restore -U bandwidth -d bandwidth --no-owner --no-acl --single-transaction --exit-on-error < "$backup_dir/bandwidth.dump"
sudo -u bandwidth .venv/bin/alembic upgrade head
```

Keep the server's database password, HTTPS setting, and paths when transferring application-specific configuration. Restore the complete accounting file and ISE contract/CA bundle before starting the worker. Sign in with the restored account credentials; another bootstrap is unnecessary. Never restore development data over an existing live database as part of a code update.

## Configure HTTPS

On this dedicated new host, obtain a certificate for your hostname. With public DNS and inbound port 80 available, first enable the HTTP-only challenge site:

```bash
sudo install -d -m 0755 /var/lib/letsencrypt/.well-known/acme-challenge
sudo cp deploy/nginx/bandwidth-engine-http.conf /etc/nginx/sites-available/bandwidth-engine
sudoedit /etc/nginx/sites-available/bandwidth-engine
sudo ln -s /etc/nginx/sites-available/bandwidth-engine /etc/nginx/sites-enabled/bandwidth-engine
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
sudo certbot certonly --webroot -w /var/lib/letsencrypt -d bandwidth.example.com
```

Replace the hostname in the HTTP site before enabling it. After Certbot succeeds, install the HTTPS site and replace `bandwidth.example.com` in `server_name` and both certificate paths. For an internal deployment, use certificates from your organization's trusted CA and configure their actual paths instead; the public ACME challenge is unnecessary.

Enable and validate HTTPS:

```bash
sudo cp deploy/nginx/bandwidth-engine.conf /etc/nginx/sites-available/bandwidth-engine
sudoedit /etc/nginx/sites-available/bandwidth-engine
sudo nginx -t
sudo systemctl reload nginx
sudo install -m 0755 deploy/nginx/reload-after-renewal.sh /etc/letsencrypt/renewal-hooks/deploy/bandwidth-nginx
sudo systemctl enable --now certbot.timer
sudo certbot renew --dry-run
```

The final HTTP site retains the webroot challenge for unattended renewal and redirects other requests to HTTPS. The deploy hook validates and reloads Nginx after certificate renewal. If you use an internal CA, follow that CA's renewal workflow instead of the Certbot commands.

The supplied proxy forwards the original host, scheme, and client address. Uvicorn trusts forwarded headers only from the loopback proxy, which preserves HTTPS origin checks and client-based login throttling. The application continues enforcing its session, role/permission, and CSRF requirements. Buffering is disabled and the proxy timeout exceeds the dashboard heartbeat interval, so `/api/events` can deliver live updates.

## Enable background services

```bash
sudo cp deploy/systemd/bandwidth-database.service deploy/systemd/bandwidth-api.service deploy/systemd/bandwidth-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bandwidth-database.service bandwidth-api.service bandwidth-worker.service
sudo systemctl status bandwidth-database.service bandwidth-api.service bandwidth-worker.service
```

The database unit waits for PostgreSQL health before the API or worker starts. API and worker run as the unprivileged `bandwidth` account and restart after process failure. PostgreSQL's restart policy and enabled systemd units cover host restarts. Durable jobs remain in PostgreSQL across worker restarts.

Open `https://bandwidth.example.com` and sign in. Check:

- `curl --fail https://bandwidth.example.com/health` returns `{"status":"ok"}`.
- The browser stays connected to live events, and permitted changes appear without reloading.
- Accounts, default plan, policies, subscriber allowances, history, and worker/checkpoint status match the intended deployment.
- Direct external connections to ports 8000 and 5432 are unavailable; clients use HTTPS.

For logs:

```bash
sudo journalctl -u bandwidth-api.service -u bandwidth-worker.service -n 100 --no-pager
sudo docker compose -f compose.yaml -f deploy/compose.ubuntu.yaml logs --tail 100 db
```

`/health` only verifies application database connectivity. It does not establish accounting freshness, worker progress, or live ISE enforcement. Inspect those separately in the dashboard. Each open dashboard event stream uses a dedicated PostgreSQL connection; size connection capacity for your expected administrators and API/worker activity.

## Connect the real accounting and ISE workflow

1. Select or create an enabled default quota plan in the dashboard.
2. Configure a trusted WLC/RADIUS accounting exporter producing the supported [RADIUS JSONL format](api.md), with stable record IDs and the original append-only file retained. Set the server's accounting adapter/file, then restart both application services.
3. Inspect the real ISE version/patch, API capabilities, active-session readback, WLC speed enforcement, and existing authorization profiles. Complete the reviewed [ISE deployment contract](ise.md) before setting `ISE_MODE=live`.
4. Restart both application services after configuration changes. Verify new subscriber discovery, quota decisions, desired versus confirmed state, and actual client-session/profile readback using an authorized test identity.

Unknown usernames in trusted accounting are registered and receive the enabled default automatically. The middleware does not provision subscriber passwords, read your separate subscriber-credential database, or receive raw syslog by itself. A successful mock response or HTTP acknowledgement is not live enforcement. The actual ISE/WLC details are still required to complete that integration.

## Update the server

Develop and run `make check` and `make test` on CachyOS, then commit and push the reviewed change. On Ubuntu, take a protected database backup before a schema update, and stop the API and worker before changing code. The database can remain running.

Because the deployment is root-owned, perform the update as an administrator. The example assumes that root's approved Git credential helper is already configured for the private HTTPS remote. To use a read-only SSH deploy key instead, configure it for root and change the checkout's remote to the matching SSH URL before pulling. Do not copy a personal GitHub token into a command or unit file.

```bash
sudo systemctl stop bandwidth-api.service bandwidth-worker.service
cd /opt/bandwidth-engine
sudo git pull --ff-only origin codex/updated-middleware-workflow
sudo "$HOME/.local/bin/uv" sync --frozen --no-dev --python /usr/bin/python3.12 --link-mode copy
sudo chown -R root:bandwidth /opt/bandwidth-engine
sudo chmod -R u=rwX,g=rX,o= /opt/bandwidth-engine
sudo chmod 640 /opt/bandwidth-engine/.env
sudo -u bandwidth .venv/bin/alembic upgrade head
sudo cp deploy/systemd/bandwidth-database.service deploy/systemd/bandwidth-api.service deploy/systemd/bandwidth-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl start bandwidth-api.service bandwidth-worker.service
```

Run each step separately and stop on errors; a failed dependency sync or migration must be resolved before starting the services. Review proxy/Compose changes and apply them deliberately rather than replacing the active hostname/certificate configuration. Check service logs, health, and the browser after restarting. The server keeps its local `.env`, PostgreSQL volume, and accounting files; a code deployment does not reset usage or assign a new allowance.

## Backups and recovery

Store regular PostgreSQL dumps and the associated accounting files/configuration on protected storage outside the server. The [backup/restore guide](cachyos.md#back-up-on-fedora-before-replacing-the-os) documents the archive commands; on Ubuntu prefix Docker operations with `sudo` and use both Compose files. Pause ingestion/export when taking a matched checkpoint/file backup. Never delete the PostgreSQL volume during an ordinary stop or code update.

After a recovery, check accounts, assignments, retained periods, checkpoints, and enforcement state before resuming live processing. Pending durable jobs resume when the worker starts, and accounting during downtime needs to be available from the source. Database backups do not replace backups of the separate subscriber-credential database or the Cisco deployment configuration.
