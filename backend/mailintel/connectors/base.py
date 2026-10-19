from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Mailbox, MessageBundle, MessageRef


class ConnectorError(RuntimeError):
    """Raised when a connector cannot fulfill a request."""

    def __init__(self, message: str, *, operator_message: str | None = None):
        super().__init__(message)
        self.operator_message = operator_message or message


class ConnectorAuthenticationError(ConnectorError):
    """Raised when credentials are missing or rejected by the provider."""


class ConnectorConnectionError(ConnectorError):
    """Raised when the provider endpoint cannot be reached."""


class ReadOnlyMailConnector(ABC):
    provider_type: str

    def __init__(self, account_settings: dict[str, object], secret_locator: str | None = None):
        self.account_settings = account_settings
        self.secret_locator = secret_locator

    @abstractmethod
    def list_accounts(self) -> list[dict[str, object]]:
        raise NotImplementedError

    @abstractmethod
    def list_mailboxes(self) -> list[Mailbox]:
        raise NotImplementedError

    @abstractmethod
    def get_sync_cursor(self, mailbox: Mailbox) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def list_message_refs(
        self,
        mailbox: Mailbox,
        cursor: str | None = None,
        unread_only: bool = True,
        limit: int = 2500,
        received_since: datetime | None = None,
    ) -> list[MessageRef]:
        raise NotImplementedError

    @abstractmethod
    def fetch_message_bundle(
        self,
        mailbox: Mailbox,
        provider_message_id: str,
        max_chars: int = 4000,
    ) -> MessageBundle:
        raise NotImplementedError
