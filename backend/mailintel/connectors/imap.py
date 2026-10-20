# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import os
import ssl
import unicodedata
from datetime import datetime
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from .base import ConnectorAuthenticationError, ConnectorConnectionError, ConnectorError, ReadOnlyMailConnector
from ..models import AttachmentDescriptor, Mailbox, MessageBundle, MessageRef, NormalizedMessage
from ..services.cleaning import clean_email_text

logger = logging.getLogger(__name__)


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    decoded_parts = email.header.decode_header(value)
    parts: list[str] = []
    for item, charset in decoded_parts:
        if isinstance(item, bytes):
            try:
                parts.append(item.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(item.decode("latin-1", errors="replace"))
        else:
            parts.append(item)
    return unicodedata.normalize("NFC", "".join(parts))


def _decode_addresses(values: list[str]) -> list[tuple[str, str]]:
    decoded_values = [_decode_header_value(value) for value in values]
    return [(name, address) for name, address in getaddresses(decoded_values)]


def _message_addresses(message: Message, header_name: str) -> list[str]:
    return [f"{name} <{address}>".strip() if name else address for name, address in _decode_addresses(message.get_all(header_name, []))]


def _extract_text_part(message: Message) -> str:
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if content_type == "text/plain" and disposition != "attachment":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except LookupError:
                    return payload.decode("latin-1", errors="replace")
        for part in message.walk():
            if part.get_content_type() == "text/html" and part.get_content_disposition() != "attachment":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except LookupError:
                    return payload.decode("latin-1", errors="replace")
        return ""
    payload = message.get_payload(decode=True) or b""
    charset = message.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("latin-1", errors="replace")


def _snippet_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _imap_since_date(value: datetime) -> str:
    normalized = value.astimezone().date() if value.tzinfo else value.date()
    return f"{normalized.day:02d}-{MONTH_NAMES[normalized.month - 1]}-{normalized.year}"


def _parse_rfc822_message_parts(
    provider_message_id: str,
    mailbox_provider_id: str,
    raw_bytes: bytes,
    unread: bool = True,
) -> tuple[NormalizedMessage, list[AttachmentDescriptor], str]:
    parsed = email.message_from_bytes(raw_bytes)
    sender_name, sender_address = ("", "")
    from_values = _decode_addresses(parsed.get_all("From", []))
    if from_values:
        sender_name, sender_address = from_values[0]
    body_text = clean_email_text(_extract_text_part(parsed).strip())
    received_at = parsedate_to_datetime(parsed.get("Date")) if parsed.get("Date") else None
    attachments: list[AttachmentDescriptor] = []
    for part in parsed.walk():
        if part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        attachments.append(
            AttachmentDescriptor(
                provider_attachment_id=part.get("Content-ID"),
                filename=part.get_filename() or "attachment.bin",
                mime_type=part.get_content_type(),
                size_bytes=len(payload),
                metadata={"content_disposition": part.get("Content-Disposition", "")},
            )
        )

    normalized = NormalizedMessage(
        provider_message_id=provider_message_id,
        mailbox_provider_id=mailbox_provider_id,
        internet_message_id=parsed.get("Message-ID"),
        thread_id=parsed.get("Thread-Index"),
        subject=_decode_header_value(parsed.get("Subject")),
        sender_name=sender_name,
        sender_address=sender_address,
        recipients=_message_addresses(parsed, "To"),
        cc=_message_addresses(parsed, "Cc"),
        received_at=received_at if isinstance(received_at, datetime) else None,
        sent_at=received_at if isinstance(received_at, datetime) else None,
        unread=unread,
        has_attachments=bool(attachments),
        importance_hint=parsed.get("Importance"),
        snippet_hash=_snippet_hash(body_text),
        metadata={"headers": {"In-Reply-To": parsed.get("In-Reply-To"), "References": parsed.get("References")}},
    )
    return normalized, attachments, body_text


def parse_rfc822_message(provider_message_id: str, mailbox_provider_id: str, raw_bytes: bytes, unread: bool = True) -> tuple[NormalizedMessage, str, list[AttachmentDescriptor]]:
    normalized, attachments, body_text = _parse_rfc822_message_parts(provider_message_id, mailbox_provider_id, raw_bytes, unread)
    return normalized, body_text, attachments


class IMAPConnector(ReadOnlyMailConnector):
    provider_type = "imap"

    def __init__(self, account_settings: dict[str, object], secret_locator: str | None = None):
        super().__init__(account_settings, secret_locator)
        self._sync_cursors: dict[str, str | None] = {}
        self._client: imaplib.IMAP4 | None = None

    def list_accounts(self) -> list[dict[str, object]]:
        return [{"provider": self.provider_type, "username": self.account_settings.get("username", "")}]

    def _create_connection(self) -> imaplib.IMAP4:
        host_raw = self._required_setting("host")
        port_val = self.account_settings.get("port", "993")
        port_raw = str(int(port_val)) if isinstance(port_val, (int, float)) else str(port_val)
        username = self._required_setting("username")
        google = self.account_settings.get("auth_mode") == "google_oauth"
        if google:
            from ..google_oauth import access_token
            password = access_token(self.account_settings)
            host_raw, port_raw = "imap.gmail.com", "993"
        else:
            password = self._resolve_secret()
        use_ssl = google or bool(self.account_settings.get("ssl", True))

        hosts = [h.strip() for h in host_raw.split(",")]
        ports = [int(p.strip()) for p in port_raw.split(",")]

        ssl_context = None
        if not google and use_ssl and bool(self.account_settings.get("ignore_tls_errors", False)):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        last_error: Exception | None = None
        for i, host in enumerate(hosts):
            port = ports[i] if i < len(ports) else ports[-1]
            logger.info("IMAP connecting: account=%s host=%s port=%s", username, host, port)
            try:
                if use_ssl:
                    if ssl_context is None:
                        client = imaplib.IMAP4_SSL(host=host, port=port)
                    else:
                        client = imaplib.IMAP4_SSL(host=host, port=port, ssl_context=ssl_context)
                else:
                    client = imaplib.IMAP4(host=host, port=port)
            except OSError as exc:
                logger.warning("IMAP connection failed: account=%s host=%s port=%s error=%s", username, host, port, exc)
                last_error = exc
                continue
            try:
                logger.info("IMAP connected: account=%s host=%s port=%s logging in", username, host, port)
                if google:
                    auth = f"user={username}\x01auth=Bearer {password}\x01\x01".encode()
                    sent = False

                    def respond(_challenge):
                        nonlocal sent
                        if sent:
                            return b""  # Google error challenge requires an empty reply.
                        sent = True
                        return auth  # imaplib handles base64 encoding.

                    result, _ = client.authenticate("XOAUTH2", respond)
                else:
                    result, _ = client.login(username, password)
            except imaplib.IMAP4.error as exc:
                try:
                    client.logout()
                except Exception:
                    pass
                if google:
                    raise ConnectorAuthenticationError("Google IMAP login failed. Check the mailbox address and use Reconnect Google.") from exc
                locator = f" in environment variable {self.secret_locator}" if self.secret_locator else ""
                server_detail = exc.args[0] if exc.args else ""
                if isinstance(server_detail, bytes):
                    server_detail = server_detail.decode("utf-8", errors="replace")
                server_detail = str(server_detail).strip()
                suffix = f" Server said: {server_detail}" if server_detail else ""
                logger.warning("IMAP login failed: account=%s host=%s port=%s error=%s", username, host, port, exc)
                raise ConnectorAuthenticationError(f"IMAP login failed for {username}. Check the password{locator}.{suffix}") from exc
            if result != "OK":
                logger.warning("IMAP login rejected: account=%s host=%s port=%s result=%s", username, host, port, result)
                raise ConnectorAuthenticationError(f"IMAP login failed for {username}.")
            logger.info("IMAP login OK: account=%s host=%s port=%s", username, host, port)
            return client

        logger.error("IMAP all hosts exhausted: account=%s hosts=%s last_error=%s", username, hosts, last_error)
        raise ConnectorConnectionError(
            f"Could not connect to IMAP server: {last_error}",
            operator_message="IMAP server is unreachable.",
        ) from last_error

    def _connect(self) -> imaplib.IMAP4:
        if self._client is not None:
            try:
                self._client.noop()
                return self._client
            except (OSError, imaplib.IMAP4.error):
                logger.info("IMAP cached connection dead, reconnecting: account=%s", self._required_setting("username"))
                self._client = None
        self._client = self._create_connection()
        return self._client

    def _resolve_secret(self) -> str:
        if self.secret_locator:
            secret = os.environ.get(self.secret_locator, "")
            if secret:
                return secret
        password = self.account_settings.get("password")
        if password:
            return str(password)
        if self.secret_locator:
            raise ConnectorAuthenticationError(
                f"IMAP password not configured. Save a password for this account or set the {self.secret_locator} environment variable before starting MailIntel."
            )
        raise ConnectorAuthenticationError("IMAP password not configured. Save a password for this account or configure a Secret locator environment variable.")

    def _required_setting(self, key: str) -> str:
        value = str(self.account_settings.get(key, "")).strip()
        if not value:
            raise ConnectorError(f"IMAP account setting '{key}' is required.")
        return value

    def list_mailboxes(self) -> list[Mailbox]:
        client = self._connect()
        result, payload = client.list()
        if result != "OK":
            raise ConnectorError("Failed to list IMAP mailboxes")
        mailboxes: list[Mailbox] = []
        for raw_line in payload:
            line = raw_line.decode("utf-8", errors="replace")
            name = line.rsplit(" ", 1)[-1].strip('"')
            mailboxes.append(
                Mailbox(
                    id=None,
                    account_id=0,
                    provider_mailbox_id=name,
                    name=name,
                    monitored=name.upper() == "INBOX",
                )
            )
        return mailboxes

    def get_sync_cursor(self, mailbox: Mailbox) -> str | None:
        return self._sync_cursors.get(mailbox.provider_mailbox_id, mailbox.sync_cursor)

    def list_message_refs(
        self,
        mailbox: Mailbox,
        cursor: str | None = None,
        unread_only: bool = True,
        limit: int = 2500,
        received_since: datetime | None = None,
    ) -> list[MessageRef]:
        client = self._connect()
        client.select(mailbox.provider_mailbox_id, readonly=True)
        search_terms: list[str] = ["UNSEEN" if unread_only else "ALL"]
        if received_since is not None:
            search_terms.extend(["SINCE", _imap_since_date(received_since)])
        result, data = client.uid("SEARCH", None, *search_terms)
        if result != "OK":
            raise ConnectorError("Failed to search IMAP mailbox")
        refs: list[MessageRef] = []
        cursor_uid = int(cursor) if cursor and str(cursor).isdigit() else None
        all_uids = [uid for uid in data[0].decode("utf-8").split() if uid]
        if cursor_uid is not None:
            all_uids = [uid for uid in all_uids if uid.isdigit() and int(uid) > cursor_uid]
        uids = all_uids
        for uid in uids:
            refs.append(MessageRef(provider_message_id=uid, unread=unread_only, sync_token=uid))
        self._sync_cursors[mailbox.provider_mailbox_id] = uids[-1] if uids else mailbox.sync_cursor
        return refs

    def fetch_message_bundle(
        self,
        mailbox: Mailbox,
        provider_message_id: str,
        max_chars: int = 4000,
    ) -> MessageBundle:
        raw = self._fetch_raw_message(mailbox, provider_message_id)
        normalized, attachments, body_text = _parse_rfc822_message_parts(provider_message_id, mailbox.provider_mailbox_id, raw)
        return MessageBundle(
            message=normalized,
            attachments=attachments,
            body_text=body_text[: max(max_chars, 0)],
            body_source_segment="imap-rfc822",
        )

    def _fetch_raw_message(self, mailbox: Mailbox, provider_message_id: str) -> bytes:
        client = self._connect()
        client.select(mailbox.provider_mailbox_id, readonly=True)
        result, data = client.uid("FETCH", provider_message_id, "(RFC822)")
        if result != "OK" or not data or not isinstance(data[0], tuple):
            raise ConnectorError(f"Failed to fetch IMAP message {provider_message_id}")
        return data[0][1]
