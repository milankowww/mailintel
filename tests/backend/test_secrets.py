# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from mailintel.account_secrets import account_for_connector, account_for_response
from mailintel.secrets import PASSWORD_ENCRYPTION_AES_256_GCM_V1, PasswordEncryptor


def test_password_encryptor_creates_key_file_and_round_trips(tmp_path):
    key_file = tmp_path / "email-password.key"
    encryptor = PasswordEncryptor(key_file)

    encrypted_password, encryption_type = encryptor.encrypt_password("correct horse battery staple")

    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600
    assert encrypted_password != "correct horse battery staple"
    assert encryption_type == PASSWORD_ENCRYPTION_AES_256_GCM_V1
    assert encryptor.decrypt_password(encrypted_password, encryption_type) == "correct horse battery staple"


def test_account_for_connector_injects_decrypted_password(tmp_path):
    encryptor = PasswordEncryptor(tmp_path / "email-password.key")
    encrypted_password, encryption_type = encryptor.encrypt_password("mail-password")

    account = account_for_connector(
        {
            "provider_type": "imap",
            "settings_json": {"host": "imap.example.com"},
            "secret_locator": None,
            "encrypted_password": encrypted_password,
            "password_encryption": encryption_type,
        },
        encryptor,
    )

    assert account["settings_json"]["password"] == "mail-password"


def test_account_for_response_hides_encrypted_password():
    response = account_for_response(
        {
            "id": 1,
            "encrypted_password": "ciphertext",
            "password_encryption": PASSWORD_ENCRYPTION_AES_256_GCM_V1,
        }
    )

    assert "encrypted_password" not in response
    assert response["has_password"] is True
