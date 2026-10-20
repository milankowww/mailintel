<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# Building and releasing MailIntel

## Build an installable release

Use Python 3.12 and Node.js 24 for the locked release environment. From the repository root, with a Python virtual environment activated:

```sh
python -m pip install --require-hashes -r backend/requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e backend
(cd frontend && npm ci && npm test && npm run build)
# Point this at a disposable PostgreSQL 16 database with pgvector.
export MAILINTEL_TEST_DATABASE_URL='postgresql://test_user:test_password@127.0.0.1:5432/mailintel_test'
python scripts/ci/wait_database.py
python -m pytest
reuse lint
python -m build --no-isolation backend --outdir dist
python scripts/ci/check_release.py
docker build -f deploy/docker/Dockerfile -t mailintel:release .
```

Build into a clean `dist/` directory, moving any existing artifacts you want to retain elsewhere first. The wheel contains the built frontend, prompt resources, migration SQL, author metadata, and EUPL text. The source distribution contains the backend and its license. The container carries the license at `/usr/share/licenses/mailintel/LICENSE` and in the installed package.

From a committed checkout, `scripts/packaging/pack-source.sh` produces a separate archive of the whole repository, including the frontend source and build instructions. Verify the installation path from that archive in an empty directory, using the Docker quick start and synthetic data.

## Version and publish

Keep the version in `backend/pyproject.toml`, `backend/mailintel/__init__.py`, `frontend/package.json`, and both root version entries in `frontend/package-lock.json` aligned. Use tags of the form `v0.1.0`. The release check verifies version alignment and checks a CI tag against the package version.

The public repository is [https://github.com/milankowww/mailintel](https://github.com/milankowww/mailintel), with `main` as the default branch. Enable GitHub Actions and require passing checks through a branch ruleset before accepting contributions. Repository visibility, branch rules, and release publication are maintainer settings on GitHub.

After reviewing the tested commit, create and push an annotated tag for its version. Publish the checked artifacts through the hosting service's release page with checksums, installation instructions, supported platforms, migration requirements, and contributor acknowledgements. Give each contributor a chance to choose their displayed name or handle. Keep the repository guides focused on current behavior; release-specific notes belong with the release.

The pipeline builds and checks artifacts; the maintainer selects the release commit and publishes it. Use a separate test installation to verify upgrades and restore a backup before recommending a release for an existing mailbox installation.

## CI setup

`.github/workflows/ci.yml` runs backend tests with disposable PostgreSQL/pgvector, frontend tests and build, REUSE checks, Python distribution validation, and a Docker startup check on GitHub-hosted Ubuntu runners. The test jobs use synthetic data and an isolated database password. Actions are pinned to commit hashes and the workflow token has read-only access to repository contents.

The package job creates `SHA256SUMS` alongside the archives and retains build artifacts for 30 days. Publish selected release artifacts through GitHub Releases for durable storage. Dependency audits run weekly and through the workflow's **Run workflow** button. Audit failures appear as failed jobs for review. Review and refresh pinned action versions along with dependencies.

## Dependencies

`frontend/package-lock.json` pins frontend packages. The three backend requirement locks pin release runtime, build, and development dependencies with hashes for Python 3.12 on Linux. The package manifest keeps supported dependency ranges for other installations. Container base image tags remain updateable; record the built image digest with a release when distributing images.

Review dependency updates before each release and regularly between releases. Install `uv` in a tooling environment, then run `scripts/ci/update-locks.sh` to refresh backend locks. It resolves runtime, build, and development sets in order. Review the diff, run the full CI checks, and commit the updated locks together. For frontend updates, run `npm update` in `frontend/`, review the manifest and lockfile, and run its tests and build.

For vulnerability review, run `pip-audit -r backend/requirements.lock --no-deps --disable-pip` from the repository root and `npm audit --audit-level=high` from `frontend/`. Assess findings for the shipped configuration, update affected dependencies, and record any justified exceptions explicitly. Coordinate exploitable application issues through [Security reporting](../SECURITY.md).
