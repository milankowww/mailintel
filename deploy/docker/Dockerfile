# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

FROM node:24-slim AS frontend-builder

WORKDIR /src

COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci

COPY frontend/ ./frontend/
COPY backend/mailintel/web/ ./backend/mailintel/web/
RUN cd frontend && npm run build


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAILINTEL_PROFILE=docker-local \
    MAILINTEL_PORT=8000 \
    MAILINTEL_CONFIG_DIR=/var/lib/mailintel/secrets

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser
RUN mkdir -p /var/lib/mailintel/secrets && chown -R appuser:appuser /var/lib/mailintel

# Install third-party packages from the manifest before copying application code.
COPY backend/pyproject.toml ./pyproject.toml
RUN python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -c 'import pathlib, tomllib; print("\n".join(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["dependencies"]))' > /tmp/mailintel-requirements.txt \
    && python -m pip install --no-cache-dir -r /tmp/mailintel-requirements.txt

COPY backend/ ./
COPY --from=frontend-builder /src/backend/mailintel/web/build ./mailintel/web/build
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation .

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "mailintel init-db && mailintel serve --host 0.0.0.0 --port ${MAILINTEL_PORT:-8000}"]
