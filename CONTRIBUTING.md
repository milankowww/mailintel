# Contributing

Follow [Installation](doc/installation.md#local-python) to install the backend and build the frontend. Keep changes focused, describe the user-visible behavior, and add tests for meaningful changes to processing, persistence, or API behavior. Use synthetic mailbox contents and placeholder credentials in fixtures.

Preserve the read-only connector boundary when adding features. Keep mailbox credentials with connectors and model operations scoped to analysis and retrieval, with results stored as application data for users to review.

## Run checks

From the repository root with the Python environment activated:

```sh
python -m pytest
(cd frontend && node --test tests/*.test.js && npm run build)
```

PostgreSQL tests skip unless `MAILINTEL_TEST_DATABASE_URL` points to a disposable PostgreSQL database with pgvector. These tests create schemas and write data. Use a separate database dedicated to tests.

```sh
export MAILINTEL_TEST_DATABASE_URL='postgresql://test_user:test_password@127.0.0.1:5432/mailintel_test'
python -m pytest
```

## Database changes

The initial revision is `0001_initial_schema`. Its SQL is frozen in `backend/mailintel/persistence/migrations/versions/0001_initial_schema.sql`. Update the current schema in `persistence/orm.py` and add a new Alembic revision for subsequent schema changes; preserve published revisions unchanged. Validate fresh initialization, upgrade behavior, and repeated initialization against PostgreSQL, including indexes and constraints.

From `backend/`, with `MAILINTEL_DATABASE_URL` set, run `alembic -c alembic.ini upgrade head` to apply revisions to a test database. The application initializer currently creates the baseline directly; future schema changes must also provide a deliberate startup upgrade path.

## Share source safely

Commit intended source changes, then run `scripts/packaging/pack-source.sh`. It uses `git archive HEAD` to include exactly the committed source tree. Output goes to ignored `dist/`.

Keep private Compose overrides in `deploy/compose/docker-compose.local.yml` and private helper directories with a `.local` suffix. Keep backups under `backups/`. Review staged files with `git diff --cached` before committing. A source archive must be built before it can serve the frontend; it intentionally excludes generated assets.
