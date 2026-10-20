<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# Operations

## Authentication and access

The API and browser use one bearer token. Print it with `mailintel auth token`, or with `docker compose --project-directory . -f deploy/compose/docker-compose.yml exec app mailintel auth token`. By default it is generated and stored in the secrets directory. Set `MAILINTEL_API_TOKEN` to supply your own token.

The default deployment binds to localhost. For remote use, provide HTTPS through a reverse proxy and restrict access to trusted users. Everyone with the bearer token has access to the installation. Protect the database, model credentials, backups, and secrets directory with filesystem and network permissions.

## Configuration

Compose reads `.env`; local Python reads exported environment variables. A variable in `.env` only reaches a container if Compose passes it through. Add extra backend settings through a private Compose override or an explicit environment entry.

| Variable | Default / purpose |
| --- | --- |
| `POSTGRES_PASSWORD` | Required by Compose |
| `MAILINTEL_PORT` | Host port 8000 |
| `MAILINTEL_DATABASE_URL` | Python default `postgresql://localhost/mailintel`; supplied by Compose |
| `MAILINTEL_CONFIG_DIR` | Python default `~/.config/mailintel`; Docker uses persisted secrets |
| `MAILINTEL_API_TOKEN` | Optional explicit bearer token |
| `MAILINTEL_ENCRYPTION_KEY_FILE` | Override `email-password.key` in the secrets directory |
| `MAILINTEL_CLUSTER_WORKER_COUNT` | 4; allowed range 1–20 |
| `MAILINTEL_LLM_TIMEOUT_SECONDS` | 1800 seconds |
| `MAILINTEL_SMART_SEARCH_STATEMENT_TIMEOUT_MS` | 60000 milliseconds per retrieval statement |
| `MAILINTEL_SMART_SEARCH_OWNER_TIMEZONE` | UTC in Compose; use an IANA timezone name |
| `MAILINTEL_SYNC_INTERVAL_SECONDS` | 120 seconds |
| `MAILINTEL_CORS_ORIGINS` | Empty unless configured; comma-separated browser origins |

The full set of backend settings and defaults is in [settings.py](../backend/mailintel/settings.py). Per-preset context budgets, concurrency, capabilities, and streaming timeouts are configured in LLM Settings.

## Back up and restore

A complete backup needs both the PostgreSQL database and the secrets directory in the `mailintel_secrets` Docker volume. The encryption key is required to decrypt stored account credentials. Preserve it alongside the database backup. Stop the app while taking a coordinated backup; keep the database running for `pg_dump`.

```sh
mkdir -p backups
docker compose --project-directory . -f deploy/compose/docker-compose.yml stop app
docker compose --project-directory . -f deploy/compose/docker-compose.yml exec -T db pg_dump -U mailintel -d mailintel -Fc > backups/mailintel.dump
docker compose --project-directory . -f deploy/compose/docker-compose.yml run --rm --no-deps --user root -v "$PWD/backups:/backup" --entrypoint tar app -C /var/lib/mailintel -czf /backup/secrets.tar.gz secrets
docker compose --project-directory . -f deploy/compose/docker-compose.yml start app
```

On a fresh installation with the same Compose files and `.env` configured, restore before starting the app:

```sh
docker compose --project-directory . -f deploy/compose/docker-compose.yml up -d db
# Wait for the database health check to pass.
docker compose --project-directory . -f deploy/compose/docker-compose.yml exec -T db pg_restore -U mailintel -d mailintel --no-owner < backups/mailintel.dump
docker compose --project-directory . -f deploy/compose/docker-compose.yml run --rm --no-deps --user root -v "$PWD/backups:/backup:ro" --entrypoint tar app -C /var/lib/mailintel -xzf /backup/secrets.tar.gz
docker compose --project-directory . -f deploy/compose/docker-compose.yml up -d app
```

Use an empty target database with pgvector available. The secrets archive preserves the original file ownership. Use `pg_dump` or a PostgreSQL-supported physical backup procedure for consistency. Keep backups private and test restoration separately.

For portable settings, use Config Backup in the UI or `mailintel config export backups/settings.backup` and `mailintel config import backups/settings.backup`. The CLI prompts for a passphrase. Configuration exports include credentials and OAuth configuration. Full database backups also preserve messages, search results, and derived data.

## Database baseline

This source tree contains one initial schema revision. New installations use `mailintel init-db`; repeated initialization of the same current schema is safe. Older pre-release databases require their original upgrade path before adopting this baseline.

For an existing installation, back up first and verify its schema matches the baseline. An Alembic-managed database with a removed pre-release revision must be reconciled before using the new revision graph. Only after checking schema equivalence, use `alembic -c alembic.ini stamp --purge 0001_initial_schema` from `backend/`. Stamping updates version bookkeeping after schema and data conversion have been verified. Retain the old source/history if an older database needs its original migrations.

## Troubleshooting

- **UI requests a token:** retrieve it from the running app's environment.
- **UI missing after Python install:** build the frontend as described in [Installation](installation.md).
- **Provider unreachable:** verify its URL from the app's network; container localhost refers to the app container. Check credentials, model names, and enabled capabilities.
- **Search has little evidence:** check monitored folders, ingestion window, summary completion, canonical embedding model, and embedding progress.
- **Repeated model errors:** check context size, output budget, and timeout settings. The queue records failures and retries; inspect its error information before raising concurrency.
- **Mail authentication fails:** check account credentials or reconnect OAuth, then retry sync.
- **Secrets permission error:** the Docker app runs as an unprivileged user. Ensure the secrets mount is writable by that user; preserve existing key files when correcting ownership.

Logs and diagnostic exports can contain sensitive mail content. Review and redact them before sharing an issue.
