from __future__ import annotations

import json

from .base import ConnectorError, ReadOnlyMailConnector
from .ews import EWSConnector
from .imap import IMAPConnector


class ConnectorRegistry:
    def __init__(self):
        self._providers: dict[str, type[ReadOnlyMailConnector]] = {
            "imap": IMAPConnector,
            "ews": EWSConnector,
        }

    def create(self, provider_type: str, settings_json: str | dict[str, object], secret_locator: str | None) -> ReadOnlyMailConnector:
        connector_cls = self._providers.get(provider_type)
        if connector_cls is None:
            raise ConnectorError(f"Unsupported provider type: {provider_type}")
        settings = settings_json if isinstance(settings_json, dict) else json.loads(settings_json or "{}")
        return connector_cls(settings, secret_locator)
