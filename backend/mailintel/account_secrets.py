from __future__ import annotations

import json
from typing import Any

from .secrets import PasswordEncryptor


def _decode_settings(settings_json: str | dict[str, object]) -> dict[str, object]:
    return dict(settings_json) if isinstance(settings_json, dict) else json.loads(settings_json or "{}")


def account_for_connector(account: dict[str, Any], encryptor: PasswordEncryptor) -> dict[str, Any]:
    prepared = dict(account)
    settings = _decode_settings(prepared.get("settings_json", {}))
    settings["auth_mode"] = prepared.get("auth_mode", "config")
    google = settings["auth_mode"] == "google_oauth"
    if google:
        from .google_oauth import load_config
        settings["google_oauth_config"] = load_config(encryptor)
    encrypted_password = prepared.get("encrypted_password")
    if encrypted_password:
        settings["google_refresh_token" if google else "password"] = encryptor.decrypt_password(
            str(encrypted_password),
            str(prepared.get("password_encryption") or ""),
        )
    prepared["settings_json"] = settings
    return prepared


def account_for_response(account: dict[str, Any]) -> dict[str, Any]:
    payload = dict(account)
    has_secret = bool(payload.pop("encrypted_password", None))
    payload["has_google_authorization"] = has_secret and payload.get("auth_mode") == "google_oauth"
    payload["has_password"] = has_secret and payload.get("auth_mode") != "google_oauth"
    return payload
