"""Google authorization and token refresh; IMAP remains the mail transport."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .connectors.base import ConnectorAuthenticationError, ConnectorConnectionError
from .secrets import PasswordEncryptor

MAIL_SCOPE = "https://mail.google.com/"
CALLBACK_PATH = "/oauth/google/callback"
TOKEN_URL = "https://oauth2.googleapis.com/token"
_CONFIG_LOCK = threading.Lock()


def validate_redirect_uri(value: str) -> str:
    uri = urlsplit(value)
    local = uri.hostname in {"localhost", "127.0.0.1", "[::1]", "::1"}
    if (uri.scheme != "https" and not (local and uri.scheme == "http")) or not uri.hostname:
        raise ValueError("Use HTTPS, or HTTP on localhost through a local connection/tunnel.")
    if uri.username or uri.password or uri.query or uri.fragment or uri.path != CALLBACK_PATH:
        raise ValueError(f"Callback URL must end with {CALLBACK_PATH}, with no query or fragment.")
    if not local and ("." not in uri.hostname or uri.hostname.endswith((".internal", ".local", ".localhost"))):
        raise ValueError("Google requires a public domain suffix; .internal and .local callbacks are not accepted. Use a domain you control or localhost through a tunnel.")
    return value


def load_config(encryptor: PasswordEncryptor) -> dict[str, str]:
    path = encryptor.key_file.parent / "google-oauth.json"
    with _CONFIG_LOCK:
        if not path.exists():
            return {}
        envelope = json.loads(path.read_text())
    return json.loads(encryptor.decrypt_password(envelope["ciphertext"], envelope["encryption"]))


def save_config(encryptor: PasswordEncryptor, config: dict[str, str]) -> None:
    path = encryptor.key_file.parent / "google-oauth.json"
    ciphertext, encryption = encryptor.encrypt_password(json.dumps(config))
    with _CONFIG_LOCK:
        pending = path.with_suffix(".pending")
        fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"ciphertext": ciphertext, "encryption": encryption}, handle)
        pending.replace(path)


def token_request(data: dict[str, str]) -> dict:
    try:
        response = httpx.post(TOKEN_URL, data=data, timeout=20)
        if response.status_code >= 500 or response.status_code == 429:
            raise ConnectorConnectionError("Google authorization is temporarily unavailable; retry later.")
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ConnectorConnectionError("Could not reach Google authorization; retry later.") from exc
    if response.is_error or payload.get("error"):
        if payload.get("error") == "invalid_grant":
            raise ConnectorAuthenticationError("Google authorization expired or was revoked. Use Reconnect Google in Mail settings.")
        raise ConnectorAuthenticationError("Google authorization failed. Check the OAuth client configuration and reconnect Google.")
    if not payload.get("access_token"):
        raise ConnectorAuthenticationError("Google did not return an access token. Reconnect Google.")
    return payload


def access_token(settings: dict) -> str:
    refresh_token = settings.get("google_refresh_token")
    config = settings.get("google_oauth_config") or {}
    if not refresh_token or not config.get("client_id") or not config.get("client_secret"):
        raise ConnectorAuthenticationError("Google is not connected. Configure Google OAuth and connect this account in Mail settings.")
    payload = token_request({"grant_type": "refresh_token", "refresh_token": refresh_token,
                             "client_id": config["client_id"], "client_secret": config["client_secret"]})
    return str(payload["access_token"])
