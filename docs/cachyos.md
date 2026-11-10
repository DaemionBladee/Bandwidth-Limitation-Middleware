# CachyOS development setup and Fedora migration

Use CachyOS as your development workstation and [Ubuntu Server](ubuntu-server.md) for the deployed middleware. Each installation has its own database, credentials, and accounting configuration. Keep development ISE enforcement on mock; parser testing can use a local accounting capture without issuing requests to the live ISE deployment.

To install and test a receiver on CachyOS, follow the [rsyslog setup and integration guide](rsyslog-setup.md). It includes the configuration-directory include and root-ownership settings used by the successful local capture test.

Bandwidth Engine uses Python 3.12+, PostgreSQL 16+, and Docker Compose v2. CachyOS is based on Arch Linux, and the application has no Fedora-specific runtime components. The dashboard is served by Python and requires no frontend build. These instructions use the supplied local Compose database; CachyOS itself has not been tested.

Choose the **fresh installation** steps for an empty workspace. For an existing installation, **back up on Fedora before replacing the OS**, then follow **restore on CachyOS**. Git contains the code; it does not contain your PostgreSQL data, passwords, or local configuration. A PostgreSQL Docker volume on a disk being erased will also be lost.

## Install prerequisites on CachyOS

Install the packages and start Docker:

```bash
sudo pacman -Syu --needed git make docker docker-compose uv
sudo systemctl enable --now docker
```

To run Docker as your login user:

```bash
sudo usermod -aG docker "$USER"
```

Sign out completely and sign in again before continuing. Membership in the Docker group grants administrative access to the host. Run the project commands as your normal user so that the checkout and Python environment remain owned by that user.

Verify the prerequisites in the new login session:

```bash
git --version
make --version
uv --version
docker compose version
docker info
```

The Compose command must report **v2**. `docker-compose` is the Arch package name; the project uses the command `docker compose`. A permission error from `docker info` usually means the new group membership is not active. A daemon connection error means Docker needs to be started.

## Get the workflow branch

```bash
git clone --branch codex/updated-middleware-workflow https://github.com/DaemionBladee/Bandwidth-Limitation-Middleware.git
cd Bandwidth-Limitation-Middleware
uv python install 3.12
UV_PYTHON=3.12 make install
```

Using a uv-managed Python 3.12 avoids depending on the rolling distribution's current Python release. Subsequent commands reuse the created `.venv`; do not copy a virtual environment from Fedora. The project's locked dependencies are installed with `uv sync --frozen`.

This is a private repository. Authenticate using an authorized GitHub credential manager, personal access token, or SSH key. GitHub account passwords cannot authenticate HTTPS Git operations. Do not put a token in the clone URL or commit it to a file.

## Fresh installation

Use this section only when starting with an empty database. If you have a backup, proceed to **restore on CachyOS** instead.

```bash
cp -n .env.example .env
chmod 600 .env
make db migrate
make bootstrap
make demo
make dev
```

`cp -n` preserves an existing `.env`. Review that file before starting. `make bootstrap` securely asks for a new administrator password and confirmation; `make dev` does not create an account. Remove any persistent `ADMIN_BOOTSTRAP_PASSWORD` setting after controlled setup. The optional demo creates a 50 GB/month policy with normal 20/10 Mbps and limited 2/1 Mbps speeds, plus `demo@example.com`.

Open <http://localhost:8000> and sign in with the administrator credentials you chose. In **Policy library**, choose an enabled default plan if you want new accounting identities to receive one automatically. See the [website guide](website-guide.md#default-plan-and-automatic-subscribers) for default-plan behavior.

The example environment uses mock accounting and mock ISE. Mock confirmation does not enforce bandwidth on a WLC. Configure a trusted accounting export and the inspected ISE integration separately using the [API](api.md) and [ISE](ise.md) documentation.

## Back up on Fedora before replacing the OS

Stop `make dev` with Ctrl+C and stop any separately started API processes and workers. Pause the accounting exporter while copying its files so that the database checkpoint and file contents describe the same point in time. Leave PostgreSQL running for the dump. Keep those processes stopped until the migration is complete; traffic during the interruption must remain available from your accounting source for later ingestion.

Run these commands from the old repository directory. Replace the example backup path with a real directory on external storage that will survive the OS installation:

```bash
backup_dir="/path/to/external-drive/bandwidth-engine-backup"
umask 077
mkdir -p "$backup_dir"
docker compose up -d --wait db
docker compose exec -T db pg_dump -U bandwidth -d bandwidth --format=custom --no-owner --no-acl > "$backup_dir/bandwidth.dump"
docker compose exec -T db pg_restore --list < "$backup_dir/bandwidth.dump" > "$backup_dir/archive-contents.txt"
cp .env "$backup_dir/.env"
git rev-parse HEAD > "$backup_dir/source-commit.txt"
```

Check that **every command succeeded**, the dump is nonempty, and `archive-contents.txt` lists the application tables before continuing. Archive listing validates the archive header and table of contents; a successful restore into an isolated PostgreSQL database is the stronger recovery check. Keep the original installation or disk until you have verified the restored application.

Copy these additional files to the same protected storage when configured:

- The complete append-only file referenced by `ACCOUNTING_FILE`, including the already processed prefix. PostgreSQL retains its ingestion checkpoint; an empty or truncated replacement file is rejected.
- The reviewed ISE contract referenced by `ISE_CONTRACT_FILE` and the CA bundle referenced by `ISE_CA_BUNDLE`.
- Any separate exporter configuration, deployment service files, and local application changes that have not been committed and pushed.

The dump includes website accounts and password hashes, roles and grants, policies and default selection, subscribers and assignments, retained usage, checkpoints, audits, and durable job/enforcement state. The `.env` and accounting exports can contain sensitive information; store and transfer the backup privately. This procedure does not back up a separate subscriber credential database or the configuration on Cisco ISE/WLC.

These commands target the supplied Compose service, database, and user, all named `bandwidth`. If `DATABASE_URL` points to an external PostgreSQL server, back up and restore that actual database with its database administrator instead. A dump of the unused local Compose database would not preserve external data.

## Restore on CachyOS

Install the prerequisites and clone the workflow branch as above. Keep all API processes, workers, and exporters stopped while restoring. Use a **fresh local Compose database**; the following commands expect no existing application tables. If a database already contains data, preserve it and choose a separate recovery target before proceeding.

Copy the saved configuration into the new checkout without replacing an existing `.env`:

```bash
backup_dir="/path/to/external-drive/bandwidth-engine-backup"
cp -n "$backup_dir/.env" .env
chmod 600 .env
make db
docker compose exec -T db pg_restore -U bandwidth -d bandwidth --no-owner --no-acl --single-transaction --exit-on-error < "$backup_dir/bandwidth.dump"
make migrate
```

Check each command's result before continuing. Restore **before** running `make dev` or `make migrate`: they create application tables that would conflict with the archive. The final `make migrate` upgrades the restored schema to the checked-out code. `--single-transaction --exit-on-error` rolls back the restore if an archive command fails. These instructions use PostgreSQL 16 on both systems; newer database archives may require a matching or newer PostgreSQL server and tools.

Review `.env` after copying, especially the database hostname and credentials. For this development workstation, set `ISE_MODE=mock`, `ACCOUNTING_SOURCE=mock`, and an empty `ACCOUNTING_FILE` before starting processes. Use the [Ubuntu guide](ubuntu-server.md) to resume a deployed live integration. A Compose database password is initialized only when its volume is first created; changing `POSTGRES_PASSWORD` later does not change an existing database password. Keep it consistent with `DATABASE_URL`.

Restore the accounting file in full, the ISE contract, and any CA bundle. Update their absolute paths in `.env` if your username or home directory changed. Restoring a checkpoint without its original accounting file can stop ingestion. Resume the exporter from the existing file without truncating it, using the same stable record IDs and source identity. Do not run the old and new workers against the live integration simultaneously.

The file-based checkpoint also stores its device/inode identity, including when the rsyslog parser consumes the raw capture. Copying a file to a new host/filesystem can change that identity even with identical contents. Reconcile the source/checkpoint before resuming file-based ingestion; see [retention and migration](rsyslog-setup.md#retention-and-migration). The development configuration above remains on mock accounting.

Once the restored configuration and files are ready:

```bash
make dev
```

Sign in with your **existing** website account and password. There is no need to create another Admin or run the demo after a restore. Workers resume retained pending jobs and accounting ingestion when started, so confirm the ISE configuration and accounting paths before starting them.

## Verify the migrated workspace

```bash
curl --fail http://localhost:8000/health
```

Then check the dashboard:

1. Sign in and verify the expected Admin/User roles and assigned permissions.
2. Verify policy values, the selected default plan, subscriber assignments, retained quota periods, usage, and change history.
3. Confirm the accounting checkpoint advances as new exported records arrive, with no unexpected duplicate usage or unknown-user errors.
4. Check requested versus confirmed ISE state and any pending enforcement attempts. A healthy website alone does not establish live network enforcement.

Changing the host OS does not change each policy's stored timezone or reset schedule. Browser display preferences are stored in the browser and may need to be selected again on the new OS.

For optional code verification, Node.js supplies the JavaScript syntax checker used by `make check`, and Chromium enables the browser test. They are not required to serve the dashboard:

```bash
sudo pacman -S --needed nodejs chromium
make check
make test
```

Tests use a separate database with an `_test` suffix, never the application database. See [verification](../README.md#verification) before running checks in a shared deployment.

## Send changes to Ubuntu Server

Develop and run checks on CachyOS, then commit and push the reviewed code to `codex/updated-middleware-workflow`. On Ubuntu, follow the [deployment update procedure](ubuntu-server.md#update-the-server). Copying a development `.env`, virtual environment, Docker volume, or database over the live server is not part of a code update. Database migrations run on Ubuntu before its API and worker restart; the server keeps its own accounts, policies, accounting checkpoints, and history.
