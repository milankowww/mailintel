from __future__ import annotations

from datetime import UTC, datetime

from mailintel.models import InterestProfile, NormalizedMessage
from mailintel.services.classification import derive_topic_cluster, match_interest_profiles


def build_message(subject: str) -> NormalizedMessage:
    return NormalizedMessage(
        provider_message_id="m-1",
        mailbox_provider_id="INBOX",
        internet_message_id="<m1@example.com>",
        thread_id="t-1",
        subject=subject,
        sender_name="Alice",
        sender_address="alice@example.com",
        recipients=["me@example.com"],
        cc=[],
        received_at=datetime(2024, 4, 1, 10, 30, tzinfo=UTC),
        sent_at=datetime(2024, 4, 1, 10, 30, tzinfo=UTC),
        unread=True,
        has_attachments=False,
        importance_hint="high",
        snippet_hash="hash",
        metadata={},
    )


def test_derive_topic_cluster_prefers_meaningful_phrase():
    cluster = derive_topic_cluster(
        build_message("Action required for product launch"),
        "The product launch deadline moved to Friday and needs legal review.",
    )

    assert cluster.name == "Product Launch"
    assert cluster.topic_key == "launch:product"
    assert cluster.description == "Messages about product launch"
    assert cluster.key_terms[:2] == ["product", "launch"]


def test_derive_topic_cluster_normalizes_common_subject_noise():
    first = derive_topic_cluster(
        build_message("Re: Product launch next steps"),
        "Please confirm the product launch timeline and owners.",
    )
    second = derive_topic_cluster(
        build_message("[External] Action required for product launch"),
        "The product launch timeline needs approval.",
    )

    assert first.name == "Product Launch"
    assert second.name == "Product Launch"
    assert first.topic_key == "launch:product"
    assert second.topic_key == "launch:product"


def test_derive_topic_cluster_preserves_diacritics_in_subject_terms():
    cluster = derive_topic_cluster(
        build_message("Bezpečnostné upozornenie"),
        "Bezpečnostné upozornenie pre interný tím.",
    )

    assert cluster.name == "Bezpečnostné Upozornenie"
    assert cluster.topic_key == "bezpečnostné:upozornenie"
    assert cluster.key_terms[:2] == ["bezpečnostné", "upozornenie"]


def test_interest_keyword_fallback_uses_signals_not_description_filler():
    profile = InterestProfile(
        id=1,
        name="Travel",
        description="Emails about travel, bookings, especially airplane tickets, boarding passes, reservations.",
        signals="boarding pass, letenka, airplane ticket, reservation, hotel",
        priority_weight=2.0,
    )
    message = build_message("Report Domain: sk-cert.sk Submitter: protection.outlook.com")
    body_text = "This is a DMARC aggregate report. For Emails received between the reporting interval."

    assert match_interest_profiles(message, body_text, [profile]) == []


def test_interest_keyword_fallback_matches_explicit_phrase_signal():
    profile = InterestProfile(
        id=1,
        name="Travel",
        description="Travel logistics.",
        signals="boarding pass, letenka, airplane ticket, reservation, hotel",
        priority_weight=2.0,
    )
    message = build_message("Your boarding pass for Vienna to Brussels")
    body_text = "Your boarding pass and reservation are ready for tomorrow's flight."

    matches = match_interest_profiles(message, body_text, [profile])

    assert len(matches) == 1
    assert matches[0].interest_profile_id == 1
    assert matches[0].primary_match is True
