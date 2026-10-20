#!/usr/bin/env sh
# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2
set -eu
cd "$(dirname "$0")/../.."
# Install uv separately. The release environment uses Python 3.12 on Linux.
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes --upgrade backend/pyproject.toml -o backend/requirements.lock
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes --upgrade backend/requirements-build.in -o backend/requirements-build.lock
uv pip compile --python-version 3.12 --python-platform linux --generate-hashes --upgrade backend/requirements-dev.in -o backend/requirements-dev.lock
