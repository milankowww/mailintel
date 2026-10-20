# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import base64
import os
from pathlib import Path

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise RuntimeError("cryptography is required for encrypted mail account passwords.") from exc


PASSWORD_ENCRYPTION_AES_256_GCM_V1 = "aes-256-gcm-v1"


class SecretEncryptionError(ValueError):
    pass


class PasswordEncryptor:
    def __init__(self, key_file: Path):
        self.key_file = key_file
        self.key = self._load_or_create_key(key_file)

    @staticmethod
    def _load_or_create_key(key_file: Path) -> bytes:
        if key_file.exists():
            return base64.urlsafe_b64decode(key_file.read_bytes().strip())
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key = os.urandom(32)
        fd = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(base64.urlsafe_b64encode(key))
            handle.write(b"\n")
        return key

    def encrypt_password(self, password: str) -> tuple[str, str]:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(nonce, password.encode("utf-8"), None)
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return token, PASSWORD_ENCRYPTION_AES_256_GCM_V1

    def decrypt_password(self, encrypted_password: str, encryption_type: str | None) -> str:
        if encryption_type != PASSWORD_ENCRYPTION_AES_256_GCM_V1:
            raise SecretEncryptionError(f"Unsupported password encryption type: {encryption_type or 'missing'}")
        try:
            payload = base64.urlsafe_b64decode(encrypted_password.encode("ascii"))
            nonce = payload[:12]
            ciphertext = payload[12:]
            return AESGCM(self.key).decrypt(nonce, ciphertext, None).decode("utf-8")
        except (InvalidTag, ValueError) as exc:
            raise SecretEncryptionError("Could not decrypt mail account password with the configured key file.") from exc
