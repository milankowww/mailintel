# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("mailintel.audit")

SENSITIVE_KEYS = {"password", "token", "authorization", "encrypted_password", "secret", "api_key"}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(part in key.lower() for part in SENSITIVE_KEYS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def audit_event(event: str, **fields: Any) -> None:
    logger.info("event=%s fields=%s", event, _redact(fields))
