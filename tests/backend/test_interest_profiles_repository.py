from __future__ import annotations

from mailintel.models import InterestMatch, InterestProfile, MessageSummary, TopicMembership
from mailintel.repositories import Repository


class FakeCursor:
    rowcount = 1

    def fetchone(self) -> dict[str, int]:
        return {"id": 42, "deleted_count": 3, "deleted_interval_summaries": 4}


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0

    def execute(self, query: str, params: tuple[object, ...]) -> FakeCursor:
        self.executed.append((query, params))
        return FakeCursor()

    def commit(self) -> None:
        self.commits += 1


def test_new_interest_profile_lets_database_generate_id():
    connection = FakeConnection()
    repository = Repository(connection)

    profile_id = repository.upsert_interest_profile(
        InterestProfile(
            id=None,
            name="Travel",
            description="Travel bookings",
            collaborators=["travel@example.com"],
            signals="flight hotel booking",
        )
    )

    query, params = connection.executed[0]
    assert profile_id == 42
    assert "COALESCE(id, nextval(pg_get_serial_sequence('interest_profiles', 'id')::regclass))" in query
    assert "ON CONFLICT (name)" in query
    assert params[0] is None
    assert params[1] == "Travel"
    assert connection.commits == 1


def test_existing_interest_profile_updates_by_id():
    connection = FakeConnection()
    repository = Repository(connection)

    profile_id = repository.upsert_interest_profile(
        InterestProfile(
            id=7,
            name="Travel renamed",
            description="Travel bookings",
            collaborators=[],
            signals="flight hotel booking",
        )
    )

    query, params = connection.executed[0]
    assert profile_id == 42
    assert "updated AS" in query
    assert "profile.id = incoming.id" in query
    assert "SET name = incoming.name" in query
    assert params[0] == 7
    assert params[1] == "Travel renamed"
    assert connection.commits == 1


def test_reset_processed_messages_for_accounts_bulk_deletes_and_clears_cursors():
    connection = FakeConnection()
    repository = Repository(connection)

    result = repository.reset_processed_messages_for_accounts([1, 2])

    query, params = connection.executed[0]
    assert result == {"deleted_messages": 3, "deleted_interval_summaries": 4}
    assert "DELETE FROM messages" in query
    assert "DELETE FROM mailbox_interval_summaries" in query
    assert "DELETE FROM topic_clusters" in query
    assert "account_id = ANY(%s::bigint[])" in query
    assert "SET sync_cursor = NULL" in query
    assert params == ([1, 2], [1, 2], [1, 2])
    assert connection.commits == 1


def test_summary_and_interest_match_writes_ignore_messages_deleted_by_reset():
    connection = FakeConnection()
    repository = Repository(connection)

    repository.upsert_summary(
        MessageSummary(
            message_id=109912,
            summary_text="Summary",
            importance_score=0.5,
            rationale="rationale",
            confidence=0.8,
            model_name="model",
        )
    )
    repository.replace_interest_matches(
        109912,
        [InterestMatch(interest_profile_id=7, score=0.9, rationale="matched", primary_match=True)],
    )

    summary_query, summary_params = connection.executed[0]
    match_query, match_params = connection.executed[3]
    assert "WHERE EXISTS (SELECT 1 FROM messages WHERE id = %s)" in summary_query
    assert summary_params[-1] == 109912
    assert "WHERE EXISTS (SELECT 1 FROM messages WHERE id = %s)" in match_query
    assert match_params == (109912, 7, 0.9, "matched", True, 109912)


def test_topic_membership_writes_ignore_messages_or_clusters_deleted_by_reset():
    connection = FakeConnection()
    repository = Repository(connection)

    repository.replace_topic_memberships(
        123370,
        [TopicMembership(topic_cluster_id=8, score=0.7, is_primary=True)],
    )

    insert_query, insert_params = connection.executed[1]
    assert "WHERE EXISTS (SELECT 1 FROM messages WHERE id = %s)" in insert_query
    assert "AND EXISTS (SELECT 1 FROM topic_clusters WHERE id = %s)" in insert_query
    assert insert_params == (123370, 8, 0.7, True, 123370, 8)
    assert connection.commits == 1


def test_delete_customization_settings_removes_profiles_and_user_profile_together():
    connection = FakeConnection()
    repository = Repository(connection)

    result = repository.delete_customization_settings()

    assert [query.strip() for query, _params in connection.executed] == [
        "DELETE FROM interest_profiles",
        "DELETE FROM user_profile",
    ]
    assert result == {"deleted_interest_profiles": 1, "deleted_user_profiles": 1}
    assert connection.commits == 1
