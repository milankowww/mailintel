# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import base64
import hashlib
import logging
import os
from urllib import error as urlerror
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from .base import ConnectorAuthenticationError, ConnectorConnectionError, ConnectorError, ReadOnlyMailConnector
from ..models import AttachmentDescriptor, Mailbox, MessageBundle, MessageRef, NormalizedMessage
from ..services.cleaning import clean_email_text

logger = logging.getLogger(__name__)

SOAP_NS = {
    "s": "http://schemas.xmlsoap.org/soap/envelope/",
    "m": "http://schemas.microsoft.com/exchange/services/2006/messages",
    "t": "http://schemas.microsoft.com/exchange/services/2006/types",
}

EWS_FIND_ITEM_PROPERTIES = """
<t:AdditionalProperties>
  <t:FieldURI FieldURI="message:IsRead"/>
  <t:FieldURI FieldURI="item:DateTimeReceived"/>
  <t:FieldURI FieldURI="item:ConversationId"/>
</t:AdditionalProperties>
"""

EWS_GET_ITEM_PROPERTIES = """
<t:AdditionalProperties>
  <t:FieldURI FieldURI="item:Subject"/>
  <t:FieldURI FieldURI="item:DateTimeReceived"/>
  <t:FieldURI FieldURI="item:DateTimeSent"/>
  <t:FieldURI FieldURI="message:InternetMessageId"/>
  <t:FieldURI FieldURI="item:ConversationId"/>
  <t:FieldURI FieldURI="message:IsRead"/>
  <t:FieldURI FieldURI="message:From"/>
  <t:FieldURI FieldURI="message:ToRecipients"/>
  <t:FieldURI FieldURI="message:CcRecipients"/>
  <t:FieldURI FieldURI="item:Importance"/>
  <t:FieldURI FieldURI="item:HasAttachments"/>
</t:AdditionalProperties>
"""


def parse_find_item_response(xml_text: str) -> list[MessageRef]:
    refs, _includes_last = parse_find_item_response_page(xml_text)
    return refs


def parse_find_item_response_page(xml_text: str) -> tuple[list[MessageRef], bool]:
    tree = ET.fromstring(xml_text)
    refs: list[MessageRef] = []
    root_folder = tree.find(".//m:RootFolder", SOAP_NS)
    includes_last = True
    if root_folder is not None:
        includes_last = root_folder.attrib.get("IncludesLastItemInRange", "true").lower() == "true"
    for item in tree.findall(".//t:Message", SOAP_NS):
        item_id = item.find("t:ItemId", SOAP_NS)
        is_read = item.findtext("t:IsRead", default="false", namespaces=SOAP_NS).lower() == "true"
        date_time = item.findtext("t:DateTimeReceived", default=None, namespaces=SOAP_NS)
        refs.append(
            MessageRef(
                provider_message_id=item_id.attrib["Id"] if item_id is not None else "",
                unread=not is_read,
                received_at=datetime.fromisoformat(date_time.replace("Z", "+00:00")) if date_time else None,
                thread_id=item.findtext("t:ConversationId", default=None, namespaces=SOAP_NS),
                sync_token=item_id.attrib.get("ChangeKey") if item_id is not None else None,
            )
        )
    return refs, includes_last


def _parse_get_item_response_parts(
    xml_text: str,
    mailbox_provider_id: str,
) -> tuple[NormalizedMessage, list[AttachmentDescriptor], str]:
    tree = ET.fromstring(xml_text)
    item = tree.find(".//t:Message", SOAP_NS)
    if item is None:
        raise ConnectorError("EWS GetItem response did not contain a message")
    item_id = item.find("t:ItemId", SOAP_NS)
    body = item.find("t:Body", SOAP_NS)
    body_text = clean_email_text((body.text or "").strip() if body is not None else "")
    subject = item.findtext("t:Subject", default="", namespaces=SOAP_NS)
    sender = item.find("t:From/t:Mailbox", SOAP_NS)
    sender_name = sender.findtext("t:Name", default="", namespaces=SOAP_NS) if sender is not None else ""
    sender_address = sender.findtext("t:EmailAddress", default="", namespaces=SOAP_NS) if sender is not None else ""
    received_at_text = item.findtext("t:DateTimeReceived", default=None, namespaces=SOAP_NS)
    sent_at_text = item.findtext("t:DateTimeSent", default=received_at_text, namespaces=SOAP_NS)
    attachments: list[AttachmentDescriptor] = []
    for attachment in item.findall(".//t:FileAttachment", SOAP_NS):
        attachments.append(
            AttachmentDescriptor(
                provider_attachment_id=attachment.findtext("t:AttachmentId", default=None, namespaces=SOAP_NS),
                filename=attachment.findtext("t:Name", default="attachment.bin", namespaces=SOAP_NS),
                mime_type=attachment.findtext("t:ContentType", default="application/octet-stream", namespaces=SOAP_NS),
                size_bytes=int(attachment.findtext("t:Size", default="0", namespaces=SOAP_NS)),
                metadata={},
            )
        )
    normalized = NormalizedMessage(
        provider_message_id=item_id.attrib["Id"] if item_id is not None else "",
        mailbox_provider_id=mailbox_provider_id,
        internet_message_id=item.findtext("t:InternetMessageId", default=None, namespaces=SOAP_NS),
        thread_id=item.findtext("t:ConversationId/t:Id", default=None, namespaces=SOAP_NS),
        subject=subject,
        sender_name=sender_name,
        sender_address=sender_address,
        recipients=[
            recipient.findtext("t:Mailbox/t:EmailAddress", default="", namespaces=SOAP_NS)
            for recipient in item.findall("t:ToRecipients/t:Mailbox", SOAP_NS)
        ],
        cc=[
            recipient.findtext("t:Mailbox/t:EmailAddress", default="", namespaces=SOAP_NS)
            for recipient in item.findall("t:CcRecipients/t:Mailbox", SOAP_NS)
        ],
        received_at=datetime.fromisoformat(received_at_text.replace("Z", "+00:00")) if received_at_text else None,
        sent_at=datetime.fromisoformat(sent_at_text.replace("Z", "+00:00")) if sent_at_text else None,
        unread=item.findtext("t:IsRead", default="false", namespaces=SOAP_NS).lower() != "true",
        has_attachments=bool(attachments),
        importance_hint=item.findtext("t:Importance", default=None, namespaces=SOAP_NS),
        snippet_hash=hashlib.sha256(body_text.encode("utf-8")).hexdigest(),
        metadata={"change_key": item_id.attrib.get("ChangeKey") if item_id is not None else None},
    )
    return normalized, attachments, body_text


def parse_get_item_response(xml_text: str, mailbox_provider_id: str) -> tuple[NormalizedMessage, str, list[AttachmentDescriptor]]:
    normalized, attachments, body_text = _parse_get_item_response_parts(xml_text, mailbox_provider_id)
    return normalized, body_text, attachments


class EWSConnector(ReadOnlyMailConnector):
    provider_type = "ews"

    def __init__(self, account_settings: dict[str, object], secret_locator: str | None = None):
        super().__init__(account_settings, secret_locator)
        self._sync_cursors: dict[str, str | None] = {}

    def list_accounts(self) -> list[dict[str, object]]:
        return [{"provider": self.provider_type, "username": self.account_settings.get("username", "")}]

    def list_mailboxes(self) -> list[Mailbox]:
        return [Mailbox(id=None, account_id=0, provider_mailbox_id="inbox", name="Inbox", monitored=True)]

    def get_sync_cursor(self, mailbox: Mailbox) -> str | None:
        return self._sync_cursors.get(mailbox.provider_mailbox_id, mailbox.sync_cursor)

    def list_message_refs(
        self,
        mailbox: Mailbox,
        cursor: str | None = None,
        unread_only: bool = True,
        limit: int = 0,
        received_since: datetime | None = None,
    ) -> list[MessageRef]:
        effective_received_since = received_since
        if cursor and effective_received_since is None:
            try:
                effective_received_since = datetime.fromisoformat(str(cursor))
            except ValueError:
                effective_received_since = None
        restriction_parts: list[str] = []
        if unread_only:
            restriction_parts.append(
                """
                <t:IsEqualTo>
                  <t:FieldURI FieldURI="message:IsRead"/>
                  <t:FieldURIOrConstant><t:Constant Value="false"/></t:FieldURIOrConstant>
                </t:IsEqualTo>
                """
            )
        if effective_received_since is not None:
            restriction_parts.append(
                f"""
                <t:IsGreaterThanOrEqualTo>
                  <t:FieldURI FieldURI="item:DateTimeReceived"/>
                  <t:FieldURIOrConstant><t:Constant Value="{effective_received_since.astimezone(UTC).isoformat().replace('+00:00', 'Z')}"/></t:FieldURIOrConstant>
                </t:IsGreaterThanOrEqualTo>
                """
            )
        restriction = ""
        if restriction_parts:
            if len(restriction_parts) == 1:
                restriction_body = restriction_parts[0]
            else:
                restriction_body = "<t:And>" + "".join(restriction_parts) + "</t:And>"
            restriction = f"<m:Restriction>{restriction_body}</m:Restriction>"
        page_size = 100
        all_refs: list[MessageRef] = []
        offset = 0
        while True:
            body = f"""
            <m:FindItem Traversal="Shallow">
              <m:ItemShape>
                <t:BaseShape>IdOnly</t:BaseShape>
                {EWS_FIND_ITEM_PROPERTIES}
              </m:ItemShape>
              <m:IndexedPageItemView MaxEntriesReturned="{page_size}" Offset="{offset}" BasePoint="Beginning"/>
              <m:ParentFolderIds><t:DistinguishedFolderId Id="inbox"/></m:ParentFolderIds>
              {restriction}
            </m:FindItem>
            """
            response = self._soap_request(body)
            page_refs, includes_last = parse_find_item_response_page(response)
            if not page_refs:
                break
            all_refs.extend(page_refs)
            if limit > 0 and len(all_refs) >= limit:
                all_refs = all_refs[:limit]
                break
            if includes_last:
                break
            offset += len(page_refs)
        if effective_received_since is not None:
            all_refs = [ref for ref in all_refs if ref.received_at is None or ref.received_at >= effective_received_since]
        cursor_candidates = [ref.received_at.astimezone(UTC).isoformat() for ref in all_refs if ref.received_at is not None]
        self._sync_cursors[mailbox.provider_mailbox_id] = max(cursor_candidates) if cursor_candidates else mailbox.sync_cursor
        return all_refs

    def fetch_message_bundle(
        self,
        mailbox: Mailbox,
        provider_message_id: str,
        max_chars: int = 4000,
    ) -> MessageBundle:
        response = self._get_item(provider_message_id)
        normalized, attachments, body_text = _parse_get_item_response_parts(response, mailbox.provider_mailbox_id)
        return MessageBundle(
            message=normalized,
            attachments=attachments,
            body_text=body_text[: max(max_chars, 0)],
            body_source_segment="ews-body",
        )

    def _get_item(self, provider_message_id: str) -> str:
        body = f"""
        <m:GetItem>
          <m:ItemShape>
            <t:BaseShape>Default</t:BaseShape>
            <t:BodyType>Text</t:BodyType>
            {EWS_GET_ITEM_PROPERTIES}
          </m:ItemShape>
          <m:ItemIds><t:ItemId Id="{provider_message_id}"/></m:ItemIds>
        </m:GetItem>
        """
        return self._soap_request(body)

    def _soap_request(self, inner_xml: str) -> str:
        endpoint = str(self.account_settings["endpoint"])
        username = str(self.account_settings["username"])
        password = self._resolve_secret()
        envelope = f"""
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                    xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
                    xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
          <s:Header>
            <t:RequestServerVersion Version="Exchange2013"/>
          </s:Header>
          <s:Body>{inner_xml}</s:Body>
        </s:Envelope>
        """.encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=envelope,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "Authorization": "Basic " + base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii"),
            },
        )
        try:
            logger.info("EWS connecting: account=%s endpoint=%s", username, endpoint)
            with urllib.request.urlopen(request, timeout=30) as response:
                logger.info("EWS request OK: account=%s", username)
                return response.read().decode("utf-8")
        except urlerror.HTTPError as exc:  # pragma: no cover - network not exercised in tests
            if exc.code in {401, 403}:
                logger.warning("EWS auth failed: account=%s status=%s", username, exc.code)
                raise ConnectorAuthenticationError(f"EWS authentication failed for {username}. Check the password. Server returned HTTP {exc.code}.") from exc
            logger.warning("EWS request failed: account=%s status=%s", username, exc.code)
            raise ConnectorError(f"EWS request failed: HTTP {exc.code} {exc.reason}") from exc
        except (urlerror.URLError, TimeoutError, OSError) as exc:  # pragma: no cover - network not exercised in tests
            logger.warning("EWS endpoint unreachable: account=%s error=%s", username, exc)
            raise ConnectorConnectionError(
                f"EWS endpoint is unreachable: {exc}",
                operator_message="EWS endpoint is unreachable.",
            ) from exc
        except Exception as exc:  # pragma: no cover - network not exercised in tests
            logger.warning("EWS request failed: account=%s error=%s", username, exc)
            raise ConnectorError(f"EWS request failed: {exc}") from exc

    def _resolve_secret(self) -> str:
        if self.secret_locator and self.secret_locator in os.environ:
            return os.environ[self.secret_locator]
        password = self.account_settings.get("password")
        if password:
            return str(password)
        raise ConnectorAuthenticationError("EWS password not configured. Save a password for this account or configure a Secret locator environment variable.")
