from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

from ..settings import AppSettings


PUBLIC_API_PATHS = {"/api/health"}


def _write_private_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
        handle.write("\n")


def get_or_create_api_token(settings: AppSettings) -> str:
    if settings.api_token:
        return settings.api_token
    if settings.api_token_file.exists():
        return settings.api_token_file.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    _write_private_file(settings.api_token_file, token)
    return token


def verify_bearer_token(settings: AppSettings, authorization_header: str | None) -> bool:
    expected = get_or_create_api_token(settings)
    if not authorization_header:
        return False
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False
    return hmac.compare_digest(token, expected)
