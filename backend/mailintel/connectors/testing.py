from __future__ import annotations

from datetime import datetime

from ..models import AttachmentDescriptor, Mailbox, MessageBundle, MessageRef, NormalizedMessage
from .base import ReadOnlyMailConnector


class StaticDatasetConnector(ReadOnlyMailConnector):
    provider_type = "static"

    def __init__(self, account_settings: dict[str, object], secret_locator: str | None = None):
        super().__init__(account_settings, secret_locator)
        self.mailboxes: list[Mailbox] = account_settings.get("mailboxes", [])  # type: ignore[assignment]
        self.messages: dict[tuple[str, str], tuple[NormalizedMessage, list[AttachmentDescriptor], str]] = account_settings.get("messages", {})  # type: ignore[assignment]

    def list_accounts(self) -> list[dict[str, object]]:
        return [{"provider": self.provider_type, "fixture": True}]

    def list_mailboxes(self) -> list[Mailbox]:
        return self.mailboxes

    def get_sync_cursor(self, mailbox: Mailbox) -> str | None:
        return mailbox.sync_cursor

    def list_message_refs(
        self,
        mailbox: Mailbox,
        cursor: str | None = None,
        unread_only: bool = True,
        limit: int = 2500,
        received_since: datetime | None = None,
    ) -> list[MessageRef]:
        refs: list[MessageRef] = []
        for (mailbox_provider_id, provider_message_id), (message, _, _) in self.messages.items():
            if mailbox_provider_id != mailbox.provider_mailbox_id:
                continue
            if unread_only and not message.unread:
                continue
            if received_since and message.received_at and message.received_at < received_since:
                continue
            refs.append(
                MessageRef(
                    provider_message_id=provider_message_id,
                    unread=message.unread,
                    received_at=message.received_at,
                    thread_id=message.thread_id,
                )
            )
        return refs[:limit]

    def fetch_message_bundle(
        self,
        mailbox: Mailbox,
        provider_message_id: str,
        max_chars: int = 4000,
    ) -> MessageBundle:
        message, attachments, body_text = self.messages[(mailbox.provider_mailbox_id, provider_message_id)]
        return MessageBundle(
            message=message,
            attachments=attachments,
            body_text=body_text[: max(max_chars, 0)],
            body_source_segment="fixture-body",
        )
