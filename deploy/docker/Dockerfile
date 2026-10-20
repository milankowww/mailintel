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

LABEL org.opencontainers.image.source="https://github.com/milankowww/mailintel" \
      org.opencontainers.image.licenses="EUPL-1.2" \
      org.opencontainers.image.authors="Milan Pikula"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAILINTEL_PROFILE=docker-local \
    MAILINTEL_PORT=8000 \
    MAILINTEL_CONFIG_DIR=/var/lib/mailintel/secrets

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin appuser
RUN mkdir -p /var/lib/mailintel/secrets && chown -R appuser:appuser /var/lib/mailintel

# Install the reviewed, hash-locked dependency sets before application code.
COPY backend/requirements.lock backend/requirements-build.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock -r requirements-build.lock

COPY LICENSE /usr/share/licenses/mailintel/LICENSE
COPY backend/ ./
COPY --from=frontend-builder /src/backend/mailintel/web/build ./mailintel/web/build
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation . \
    && python -m pip check

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "mailintel init-db && mailintel serve --host 0.0.0.0 --port ${MAILINTEL_PORT:-8000}"]
