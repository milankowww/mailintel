<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

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

## Copyright and license notices

Add `SPDX-FileCopyrightText` and `SPDX-License-Identifier` notices (using `EUPL-1.2`) to new project files using the file format’s comment syntax. Keep executable shebangs on the first line. Preserve existing third-party notices when incorporating code.

Use `REUSE.toml` annotations for files that require external notices, such as JSON and frozen migration SQL. The complete license text is in `LICENSES/EUPL-1.2.txt`, with identical copies in the root `LICENSE` for repository discovery and `backend/LICENSE` for Python packaging. Keep both copies unchanged and in sync.

Install the REUSE tool with `python -m pip install reuse` in your development environment and run `reuse lint` from the repository root to check notice coverage.

## Contribution credit

Every accepted contribution matters, including a typo fix, a focused test, a useful bug report, or help reproducing an issue. Preserve the contributor’s Git author identity when applying or merging changes. For jointly authored or squashed work, retain the original authors and use agreed `Co-authored-by` commit trailers where appropriate.

Use release acknowledgements to credit reports, testing, design discussion, translations, and other help that Git history does not capture. Ask for the contributor’s preferred public name or handle and consent before listing them. Security reporter credit follows the private disclosure process in [SECURITY.md](SECURITY.md).

Source-file copyright notices describe copyright holders. Add an appropriate notice when contributing copyrightable work and preserve existing notices; use commit and release attribution to credit minor edits. A contributor’s presence in an acknowledgement list does not imply ownership of every file. See the [REUSE guidance](https://reuse.software/faq/#what-is-a-copyright-holder-and-what-is-an-author) for this distinction.

## Release checks

Use the locked Python 3.12 environment and artifact checks in [Releases](doc/releases.md) when preparing a release. CI uses GitHub Actions; issue and pull-request templates are under `.github/`. Report security issues privately to [milan.pikula@ipsec.info](mailto:milan.pikula@ipsec.info).
