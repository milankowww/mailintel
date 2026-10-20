# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import base64
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from ..google_oauth import load_config as load_google_config, save_config as save_google_config
from ..models import ALL_PRESET_CAPABILITIES, InterestProfile, LLMPreset, MailAccount, PipelineSettings, UserProfile
from ..repositories import Repository
from ..secrets import SecretEncryptionError, PasswordEncryptor


BACKUP_FORMAT = "mailintel-config-backup-v1"
KDF_ITERATIONS = 390_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    ).derive(passphrase.encode("utf-8"))


def _json_field(row: dict[str, Any], field: str, default: Any) -> Any:
    value = row.get(field)
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _account_password(account: dict[str, Any], encryptor: PasswordEncryptor) -> str | None:
    encrypted_password = account.get("encrypted_password")
    if not encrypted_password:
        return None
    try:
        return encryptor.decrypt_password(str(encrypted_password), str(account.get("password_encryption") or ""))
    except SecretEncryptionError:
        return None


def build_config_payload(repository: Repository, encryptor: PasswordEncryptor) -> dict[str, Any]:
    accounts = []
    for row in repository.list_mail_accounts():
        account = dict(row)
        account_id = int(account["id"])
        accounts.append(
            {
                "name": account["name"],
                "provider_type": account["provider_type"],
                "auth_mode": account["auth_mode"],
                "secret_locator": account.get("secret_locator"),
                "settings": _json_field(account, "settings_json", {}),
                "status": account.get("status", "active"),
                "password": _account_password(account, encryptor),
                "mailboxes": [
                    {
                        "provider_mailbox_id": mailbox["provider_mailbox_id"],
                        "name": mailbox["name"],
                        "monitored": bool(mailbox["monitored"]),
                    }
                    for mailbox in repository.list_mailboxes(account_id=account_id)
                ],
            }
        )

    profile = repository.get_user_profile()
    llm_presets = [dict(row) for row in repository.list_llm_presets()]
    pipeline_settings = {"canonical_embedding_model": repository.get_pipeline_settings().get("canonical_embedding_model", "")}
    return {
        "format": BACKUP_FORMAT,
        "google_oauth": load_google_config(encryptor),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "user_profile": dict(profile) if profile else None,
        "interest_profiles": [dict(row) for row in repository.list_interest_profiles()],
        "llm_presets": llm_presets,
        "pipeline_settings": pipeline_settings,
    }


def export_config(
    repository: Repository,
    encryptor: PasswordEncryptor,
    output_path: Path,
    passphrase: str,
) -> dict[str, Any]:
    payload = build_config_payload(repository, encryptor)
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, json.dumps(payload, default=str).encode("utf-8"), None)
    envelope = {
        "format": BACKUP_FORMAT,
        "kdf": "pbkdf2-sha256",
        "iterations": KDF_ITERATIONS,
        "salt": _b64encode(salt),
        "nonce": _b64encode(nonce),
        "ciphertext": _b64encode(ciphertext),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return {
        "accounts": len(payload["accounts"]),
        "interest_profiles": len(payload["interest_profiles"]),
        "llm_presets": len(payload["llm_presets"]),
    }


def load_config_backup(input_path: Path, passphrase: str) -> dict[str, Any]:
    envelope = json.loads(input_path.read_text(encoding="utf-8"))
    if envelope.get("format") != BACKUP_FORMAT:
        raise ValueError("Unsupported MailIntel config backup format")
    key = _derive_key(passphrase, _b64decode(str(envelope["salt"])))
    plaintext = AESGCM(key).decrypt(_b64decode(str(envelope["nonce"])), _b64decode(str(envelope["ciphertext"])), None)
    payload = json.loads(plaintext.decode("utf-8"))
    if payload.get("format") != BACKUP_FORMAT:
        raise ValueError("Unsupported MailIntel config payload format")
    return payload


def import_config(
    repository: Repository,
    encryptor: PasswordEncryptor,
    input_path: Path,
    passphrase: str,
) -> dict[str, int]:
    payload = load_config_backup(input_path, passphrase)
    if payload.get("google_oauth"):
        save_google_config(encryptor, payload["google_oauth"])
    imported_accounts = 0
    unresolved_mailboxes = 0

    existing_accounts = {
        (str(row["name"]), str(row["provider_type"])): int(row["id"])
        for row in repository.list_mail_accounts()
    }
    for account in payload.get("accounts", []):
        key = (str(account["name"]), str(account["provider_type"]))
        password = account.get("password")
        encrypted_password = None
        password_encryption = None
        if password:
            encrypted_password, password_encryption = encryptor.encrypt_password(str(password))
        account_id = repository.upsert_mail_account(
            MailAccount(
                id=existing_accounts.get(key),
                name=str(account["name"]),
                provider_type=str(account["provider_type"]),
                auth_mode=str(account.get("auth_mode") or "config"),
                secret_locator=account.get("secret_locator"),
                settings=dict(account.get("settings") or {}),
                encrypted_password=encrypted_password,
                password_encryption=password_encryption,
                status=str(account.get("status") or "active"),
            )
        )
        imported_accounts += 1
        mailbox_rows = {
            str(row["provider_mailbox_id"]): int(row["id"])
            for row in repository.list_mailboxes(account_id=account_id)
        }
        for mailbox in account.get("mailboxes", []):
            mailbox_id = mailbox_rows.get(str(mailbox.get("provider_mailbox_id")))
            if mailbox_id is None:
                unresolved_mailboxes += 1
                continue
            repository.update_mailbox_monitoring(mailbox_id, bool(mailbox.get("monitored", True)))

    profile = payload.get("user_profile")
    if isinstance(profile, dict):
        repository.upsert_user_profile(
            UserProfile(
                name=str(profile.get("name") or ""),
                role=str(profile.get("role") or ""),
                organization=str(profile.get("organization") or ""),
                aliases=list(_json_field(profile, "aliases_json", [])),
                email_addresses=list(_json_field(profile, "email_addresses_json", [])),
                collaborators=list(_json_field(profile, "collaborators_json", [])),
                communication_preferences=str(profile.get("communication_preferences") or ""),
                about=str(profile.get("about") or ""),
            )
        )

    imported_interests = 0
    for row in payload.get("interest_profiles", []):
        repository.upsert_interest_profile(
            InterestProfile(
                id=None,
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                collaborators=list(_json_field(row, "collaborators_json", [])),
                signals=str(row.get("signals") or ""),
                priority_weight=float(row.get("priority_weight") or 1.0),
                dashboard_prominence=int(row.get("dashboard_prominence") or 1),
                summary_detail=str(row.get("summary_detail") or "standard"),
                watch_mode=str(row.get("watch_mode") or "watch"),
            )
        )
        imported_interests += 1

    imported_presets = 0
    for row in _preset_import_rows(payload.get("llm_presets", [])):
        repository.upsert_llm_preset(
            LLMPreset(
                id=None,
                name=str(row["name"]),
                provider=str(row.get("provider") or "ollama"),
                enabled=bool(row.get("enabled")),
                priority=int(row.get("priority") or 100),
                auth_mode=str(row.get("auth_mode") or "env"),
                secret_locator=str(row.get("secret_locator") or ""),
                endpoint_url=str(row.get("endpoint_url") or "http://127.0.0.1:11434"),
                text_model=str(row.get("text_model", "llama3.1")),
                embedding_model=str(row.get("embedding_model", "nomic-embed-text")),
                temperature=float(row.get("temperature") or 0.2),
                text_context_window=int(row.get("text_context_window") or 8192),
                embedding_context_window=int(row.get("embedding_context_window") or 8192),
                max_output_tokens=int(row.get("max_output_tokens") or 800),
                initial_timeout_seconds=float(row["initial_timeout_seconds"]) if row.get("initial_timeout_seconds") is not None else None,
                streaming_timeout_seconds=float(row["streaming_timeout_seconds"]) if row.get("streaming_timeout_seconds") is not None else None,
                notes=str(row.get("notes") or ""),
                concurrency=int(row.get("concurrency", 1)),
                capabilities=[name for name in row.get("capabilities_json", row.get("capabilities", ALL_PRESET_CAPABILITIES)) if name != "chat"],
            )
        )
        imported_presets += 1

    pipeline_settings = payload.get("pipeline_settings")
    if isinstance(pipeline_settings, dict):
        repository.update_pipeline_settings(
            PipelineSettings(canonical_embedding_model=str(pipeline_settings.get("canonical_embedding_model") or ""))
        )

    return {
        "accounts": imported_accounts,
        "interest_profiles": imported_interests,
        "llm_presets": imported_presets,
        "unresolved_mailboxes": unresolved_mailboxes,
    }


def _preset_import_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve old backup task routing while removing per-task model fields."""
    result = []
    names = {str(row["name"]) for row in rows}
    summary_tasks = {"mail_summary", "daily_summary", "rollup_summary"}
    text_tasks = {"topic_label", "search_plan", "search_compress", "search_answer"}
    for incoming in rows:
        row = dict(incoming)
        if "text_model" not in row:
            row["text_model"] = row.get("chat_model", "llama3.1")
            row["text_context_window"] = row.get("context_window") or 8192
            row["embedding_context_window"] = row.get("context_window") or 8192
            summary_model = row.get("summary_model", "llama3.1")
            capabilities = [name for name in row.get("capabilities_json", row.get("capabilities", ALL_PRESET_CAPABILITIES)) if name != "chat"]
            if not summary_model:
                capabilities = [name for name in capabilities if name not in summary_tasks]
            if not row["text_model"]:
                capabilities = [name for name in capabilities if name not in text_tasks]
            row["capabilities_json"] = capabilities
            summaries = [name for name in capabilities if name in summary_tasks]
            if summary_model and summaries and summary_model != row["text_model"]:
                if not row["text_model"] or not text_tasks.intersection(capabilities):
                    row["text_model"] = summary_model
                else:
                    split = dict(row)
                    split_name = str(row["name"]) + " [summaries]"
                    suffix = 0
                    while split_name in names:
                        suffix += 1
                        split_name = str(row["name"]) + f" [summaries {suffix}]"
                    names.add(split_name)
                    concurrency = int(row.get("concurrency", 1))
                    split.update(name=split_name, text_model=summary_model, embedding_model="",
                                 capabilities_json=summaries, concurrency=max(1, concurrency // 2))
                    row.update(capabilities_json=[name for name in capabilities if name not in summary_tasks],
                               concurrency=max(1, concurrency - split["concurrency"]))
                    result.append(split)
        result.append(row)
    return result
