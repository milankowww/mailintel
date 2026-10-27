# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from mailintel.connectors.ews import EWSConnector, parse_find_item_response, parse_get_item_response
from mailintel.connectors.base import ConnectorAuthenticationError, ConnectorError
from mailintel.connectors.imap import IMAPConnector, parse_rfc822_message
from mailintel.models import Mailbox


def test_parse_rfc822_message_normalizes_email():
    raw = (
        b"From: Alice Example <alice@example.com>\r\n"
        b"To: Bob Example <bob@example.com>\r\n"
        b"Subject: Quarterly launch update\r\n"
        b"Date: Mon, 01 Apr 2024 10:30:00 +0000\r\n"
        b"Message-ID: <abc@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"The launch is on schedule. Action required by Friday.\r\n"
    )
    message, body_text, attachments = parse_rfc822_message("100", "INBOX", raw)
    assert message.provider_message_id == "100"
    assert message.subject == "Quarterly launch update"
    assert body_text.startswith("The launch is on schedule")
    assert attachments == []


def test_parse_rfc822_message_decodes_utf8_encoded_subject():
    raw = (
        b"From: =?utf-8?Q?J=C3=A1n_Example?= <jan@example.com>\r\n"
        b"To: Bob Example <bob@example.com>\r\n"
        b"Subject: =?utf-8?Q?Bezpe=C4=8Dnostn=C3=A9_upozornenie?=\r\n"
        b"Date: Mon, 01 Apr 2024 10:30:00 +0000\r\n"
        b"Message-ID: <abc@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Obsah spravy.\r\n"
    )
    message, _, _ = parse_rfc822_message("100", "INBOX", raw)
    assert message.subject == "Bezpečnostné upozornenie"
    assert message.sender_name == "Ján Example"


def test_imap_html_only_message_preserves_meaningful_links():
    raw = (
        b"From: SOC <soc@example.com>\r\n"
        b"To: Me <me@example.com>\r\n"
        b"Subject: Incident update\r\n"
        b"Date: Mon, 01 Apr 2024 10:30:00 +0000\r\n"
        b"Message-ID: <html@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><body><p>Review the <a href=\"https://portal.example/case/42\">case details</a>.</p></body></html>\r\n"
    )

    _message, body_text, _attachments = parse_rfc822_message("100", "INBOX", raw)

    assert "Review the case details (https://portal.example/case/42)." in body_text
    assert "<a href" not in body_text


def test_imap_bundle_uses_body_limit(monkeypatch):
    body = "A" * 7000
    raw = (
        b"From: Alice Example <alice@example.com>\r\n"
        b"To: Bob Example <bob@example.com>\r\n"
        b"Subject: Long body\r\n"
        b"Date: Mon, 01 Apr 2024 10:30:00 +0000\r\n"
        b"Message-ID: <long@example.com>\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        + body.encode("utf-8")
    )
    connector = IMAPConnector({"host": "imap.example", "username": "alice", "password": "secret"})
    monkeypatch.setattr(connector, "_fetch_raw_message", lambda _mailbox, _provider_message_id: raw)

    bundle = connector.fetch_message_bundle(
        Mailbox(id=1, account_id=1, provider_mailbox_id="INBOX", name="Inbox"),
        "100",
        max_chars=6000,
    )

    assert len(bundle.body_text) == 6000
    assert bundle.body_source_segment == "imap-rfc822"


def test_imap_login_error_includes_server_detail(monkeypatch):
    class FakeIMAP:
        class error(Exception):
            pass

        def __init__(self, host, port):
            self.host = host
            self.port = port

        def login(self, _username, _password):
            raise self.error(b"[ALERT] Application-specific password required")

        def logout(self):
            pass

    monkeypatch.setattr("mailintel.connectors.imap.imaplib.IMAP4_SSL", FakeIMAP)
    monkeypatch.setattr("mailintel.connectors.imap.imaplib.IMAP4.error", FakeIMAP.error)
    connector = IMAPConnector(
        {
            "host": "imap.gmail.com",
            "port": 993,
            "username": "user@gmail.com",
            "password": "regular-password",
        }
    )

    try:
        connector.list_mailboxes()
    except ConnectorAuthenticationError as exc:
        assert "Application-specific password required" in str(exc)
    else:
        raise AssertionError("expected ConnectorError")


def test_parse_ews_responses():
    find_item_xml = """
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
      <s:Body>
        <t:Message>
          <t:ItemId Id="ews-1" ChangeKey="ck1"/>
          <t:IsRead>false</t:IsRead>
          <t:DateTimeReceived>2024-04-01T10:30:00Z</t:DateTimeReceived>
        </t:Message>
      </s:Body>
    </s:Envelope>
    """
    refs = parse_find_item_response(find_item_xml)
    assert refs[0].provider_message_id == "ews-1"
    assert refs[0].unread is True
    assert refs[0].received_at is not None

    get_item_xml = """
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
      <s:Body>
        <t:Message>
          <t:ItemId Id="ews-1" ChangeKey="ck1"/>
          <t:InternetMessageId>&lt;xyz@example.com&gt;</t:InternetMessageId>
          <t:ConversationId><t:Id>conv-1</t:Id></t:ConversationId>
          <t:Subject>Board agenda</t:Subject>
          <t:From><t:Mailbox><t:Name>CEO</t:Name><t:EmailAddress>ceo@example.com</t:EmailAddress></t:Mailbox></t:From>
          <t:DateTimeReceived>2024-04-01T10:30:00Z</t:DateTimeReceived>
          <t:IsRead>false</t:IsRead>
          <t:Importance>High</t:Importance>
          <t:Body BodyType="Text">Agenda draft for tomorrow.</t:Body>
        </t:Message>
      </s:Body>
    </s:Envelope>
    """
    message, body_text, attachments = parse_get_item_response(get_item_xml, "inbox")
    assert message.recipients == []
    assert message.cc == []
    assert message.subject == "Board agenda"
    assert message.sender_address == "ceo@example.com"
    assert message.received_at is not None
    assert body_text == "Agenda draft for tomorrow."
    assert attachments == []


def test_ews_requests_date_fields(monkeypatch):
    captured: list[str] = []
    connector = EWSConnector({"endpoint": "https://example.invalid/EWS", "username": "user", "password": "secret"})

    def fake_soap_request(inner_xml: str) -> str:
        captured.append(inner_xml)
        return """
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                    xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
          <s:Body />
        </s:Envelope>
        """

    monkeypatch.setattr(connector, "_soap_request", fake_soap_request)
    connector.list_message_refs(type("MailboxStub", (), {"provider_mailbox_id": "inbox", "sync_cursor": None})())
    connector._get_item("ews-1")

    assert 'FieldURI="item:DateTimeReceived"' in captured[0]
    assert 'FieldURI="item:DateTimeReceived"' in captured[1]
    assert 'FieldURI="item:DateTimeSent"' in captured[1]


def test_ews_paginates_until_root_folder_says_last_page(monkeypatch):
    connector = EWSConnector({"endpoint": "https://example.invalid/EWS", "username": "user", "password": "secret"})
    captured_offsets: list[str] = []
    pages = [
        """
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                    xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
                    xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
          <s:Body>
            <m:FindItemResponse>
              <m:RootFolder IncludesLastItemInRange="false" TotalItemsInView="3">
                <t:Items>
                  <t:Message><t:ItemId Id="ews-1" ChangeKey="ck1"/><t:IsRead>false</t:IsRead></t:Message>
                  <t:Message><t:ItemId Id="ews-2" ChangeKey="ck2"/><t:IsRead>false</t:IsRead></t:Message>
                </t:Items>
              </m:RootFolder>
            </m:FindItemResponse>
          </s:Body>
        </s:Envelope>
        """,
        """
        <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                    xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
                    xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
          <s:Body>
            <m:FindItemResponse>
              <m:RootFolder IncludesLastItemInRange="true" TotalItemsInView="3">
                <t:Items>
                  <t:Message><t:ItemId Id="ews-3" ChangeKey="ck3"/><t:IsRead>false</t:IsRead></t:Message>
                </t:Items>
              </m:RootFolder>
            </m:FindItemResponse>
          </s:Body>
        </s:Envelope>
        """,
    ]

    def fake_soap_request(inner_xml: str) -> str:
        offset_start = inner_xml.index('Offset="') + len('Offset="')
        offset_end = inner_xml.index('"', offset_start)
        captured_offsets.append(inner_xml[offset_start:offset_end])
        return pages.pop(0)

    monkeypatch.setattr(connector, "_soap_request", fake_soap_request)

    refs = connector.list_message_refs(Mailbox(id=1, account_id=1, provider_mailbox_id="inbox", name="Inbox"))

    assert [ref.provider_message_id for ref in refs] == ["ews-1", "ews-2", "ews-3"]
    assert captured_offsets == ["0", "2"]


def test_ews_bundle_uses_body_limit(monkeypatch):
    body = "B" * 7000
    connector = EWSConnector({"endpoint": "https://example.invalid/EWS", "username": "user", "password": "secret"})
    response = f"""
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
      <s:Body>
        <t:Message>
          <t:ItemId Id="ews-1" ChangeKey="ck1"/>
          <t:Subject>Long EWS body</t:Subject>
          <t:Body BodyType="Text">{body}</t:Body>
        </t:Message>
      </s:Body>
    </s:Envelope>
    """
    monkeypatch.setattr(connector, "_get_item", lambda _provider_message_id: response)

    bundle = connector.fetch_message_bundle(
        Mailbox(id=1, account_id=1, provider_mailbox_id="inbox", name="Inbox"),
        "ews-1",
        max_chars=6000,
    )

    assert len(bundle.body_text) == 6000
    assert bundle.body_source_segment == "ews-body"


def test_ews_parses_all_to_and_cc_recipients_from_mailbox_elements():
    xml = """
    <s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
      <s:Body><t:Message>
        <t:ItemId Id="recipient-test"/>
        <t:ToRecipients>
          <t:Mailbox><t:Name>Alice</t:Name><t:EmailAddress>alice@example.com</t:EmailAddress></t:Mailbox>
          <t:Mailbox><t:EmailAddress>bob@example.com</t:EmailAddress></t:Mailbox>
        </t:ToRecipients>
        <t:CcRecipients>
          <t:Mailbox><t:Name>Carol</t:Name><t:EmailAddress>carol@example.com</t:EmailAddress></t:Mailbox>
          <t:Mailbox><t:EmailAddress>dave@example.com</t:EmailAddress></t:Mailbox>
        </t:CcRecipients>
        <t:Body BodyType="Text">Synthetic recipient regression test.</t:Body>
      </t:Message></s:Body>
    </s:Envelope>
    """
    message, _, _ = parse_get_item_response(xml, "inbox")
    assert message.recipients == ["alice@example.com", "bob@example.com"]
    assert message.cc == ["carol@example.com", "dave@example.com"]
