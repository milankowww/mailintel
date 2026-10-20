# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import os
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from mailintel.db import connect_database, init_database
from mailintel.models import InterestProfile, JobRecord, LLMPreset, MailAccount, Mailbox, MessageSummary, NormalizedMessage
from mailintel.repositories import Repository
from mailintel.services.topics import INCREMENTAL_CLUSTERING_ALGORITHM_VERSION, TopicClusteringService


def build_repository() -> Repository:
    database_url = os.environ.get("MAILINTEL_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("MAILINTEL_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    connection = connect_database(database_url)
    init_database(connection)
    schema = f"test_{uuid.uuid4().hex}"
    connection.execute(f"CREATE SCHEMA {schema}")
    connection.execute(f"SET search_path TO {schema}, public")
    init_database(connection)
    return Repository(connection)


def seed_message(repository: Repository, *, provider_message_id: str, subject: str = "Subject") -> int:
    account_id = repository.upsert_mail_account(
        MailAccount(id=None, name="Inbox", provider_type="imap", auth_mode="env", secret_locator="MAIL_PASSWORD", settings={})
    )
    mailbox_id = repository.upsert_mailbox(Mailbox(id=None, account_id=account_id, provider_mailbox_id="INBOX", name="Inbox"))
    message_id = repository.upsert_message(
        mailbox_id,
        NormalizedMessage(
            provider_message_id=provider_message_id,
            mailbox_provider_id="INBOX",
            internet_message_id=f"<{provider_message_id}@example.com>",
            thread_id=f"thread-{provider_message_id}",
            subject=subject,
            sender_name="Sender",
            sender_address="sender@example.com",
            recipients=["me@example.com"],
            cc=[],
            received_at=datetime(2026, 4, 1, tzinfo=UTC),
            sent_at=datetime(2026, 4, 1, tzinfo=UTC),
            unread=True,
            has_attachments=False,
            importance_hint=None,
            snippet_hash=f"hash-{provider_message_id}",
        ),
        "Excerpt text",
        [],
    )
    repository.upsert_message_body_cache(message_id, "Body text for analysis")
    return message_id


def seed_ready_embedding(
    repository: Repository,
    *,
    provider_message_id: str,
    subject: str,
    embedding_model: str,
    vector: list[float],
) -> int:
    message_id = seed_message(repository, provider_message_id=provider_message_id, subject=subject)
    summary_text = f"Summary for {subject}"
    repository.upsert_summary(
        MessageSummary(
            message_id=message_id,
            summary_text=summary_text,
            importance_score=0.8,
            rationale="incremental clustering test",
            confidence=0.9,
            model_name="test",
        )
    )
    repository.complete_analysis(
        message_id,
        interest_input_hash=f"interest-{message_id}",
        summary_input_hash=f"summary-{message_id}",
        preset_name="test",
        summary_needs_refresh=False,
    )
    input_text = f"embedding input {message_id} {summary_text}"
    repository.upsert_message_embedding(
        message_id=message_id,
        preset_name="test",
        embedding_model=embedding_model,
        input_text=input_text,
        vector=vector,
    )
    repository.complete_embedding(
        message_id,
        input_text=input_text,
        embedding_model=embedding_model,
        preset_name="test",
    )
    return message_id


@pytest.mark.parametrize("explicit_terms", [True, False])
def test_smart_search_distinct_keywords_preserve_phrases_and_beat_repetition(explicit_terms):
    repository = build_repository()
    subjects = [
        "CyberTech Europe invitation dates",
        "CyberTech Europe invitation",
        "invitation invitation invitation invitation invitation",
        "Europe CyberTech unrelated",
        "CyberTech exciting Europe unrelated",
    ]
    ids = [seed_ready_embedding(repository, provider_message_id=f"keyword-{index}",
        subject=subject, embedding_model="keyword-test", vector=[1.0, 0.1])
        for index, subject in enumerate(subjects)]
    job_id = repository.create_job(JobRecord(id=None, job_type="smart_search"))
    repository._execute("INSERT INTO smart_search_jobs (job_id,question,status) VALUES (%s,%s,'running')",
        (job_id, "CyberTech Europe invitation dates"))
    repository.snapshot_smart_search_corpus(job_id, embedding_model="keyword-test")
    kwargs = {"keyword_terms": ["CyberTech Europe", "invitation", "dates", " INVITATION "]} if explicit_terms else {}
    rows = repository.search_smart_search_snapshot(job_id,
        embedding_model="keyword-test", query_text='"CyberTech Europe" invitation dates',
        exact_phrases=["CyberTech Europe", "cybertech europe"], query_vector=[1.0, 0.1],
        limit=10, statement_timeout_ms=10000, **kwargs)
    assert [row["id"] for row in rows[:3]] == ids[:3]
    assert {row["id"]: row["keyword_match_count"] for row in rows} == dict(zip(ids, [3, 2, 1, 0, 0]))
    assert all(row["lexical_rank"] is None for row in rows[3:])
    # The candidate cutoff also respects distinct matches, even though every
    # message has the same vector similarity.
    top = repository.search_smart_search_snapshot(job_id,
        embedding_model="keyword-test", query_text='"CyberTech Europe" invitation dates',
        exact_phrases=[], query_vector=[1.0, 0.1], limit=1,
        statement_timeout_ms=10000, **kwargs)
    assert top[0]["id"] == ids[0]
    repository.connection.close()


def test_early_matches_survive_summary_failure_and_retry_without_retrieval():
    from mailintel.services.smart_search import SmartSearchEngine, SmartSearchRuntime
    from mailintel.settings import AppSettings
    from mailintel.api.routers.smart_search import _public_job

    repository = build_repository()
    lexical_id = seed_message(repository, provider_message_id="preview-only", subject="CyberTech Europe invitation")
    semantic_id = seed_ready_embedding(repository, provider_message_id="semantic", subject="Related discussion",
        embedding_model="preview-test", vector=[1.0, 0.1])
    lexical = repository.search_smart_search_preview(keyword_terms=["CyberTech Europe", "invitation", "INVITATION"],
        limit=10, statement_timeout_ms=10000)
    assert [row["id"] for row in lexical] == [lexical_id]
    assert lexical[0]["keyword_match_count"] == 2
    semantic = repository.search_smart_search_preview(keyword_terms=[], query_vector=[1.0, 0.1],
        embedding_model="preview-test", limit=10, statement_timeout_ms=10000)
    assert [row["id"] for row in semantic] == [semantic_id]
    job_id = repository.create_job(JobRecord(id=None, job_type="smart_search"))
    repository._execute("INSERT INTO smart_search_jobs (job_id,question,status) VALUES (%s,'CyberTech Europe','running')", (job_id,))
    for rows in (lexical, semantic, lexical):
        repository.merge_smart_search_matches(job_id, [SmartSearchEngine._source(row, search_relevance="matching") for row in rows])
    work = {"rows": lexical, "plan": {}, "reference_time": "2026-10-12T09:00:00+00:00", "trace": {}}
    SmartSearchEngine._write_work(repository, job_id, work)
    repository.fail_smart_search_job(job_id, failed_stage="compression", error_code="compression_failed",
        safe_error_text="Internal failure detail", retryable=True, stage_trace={})
    failed = _public_job(repository.get_smart_search_job(job_id))
    assert {source["id"] for source in failed["matching_sources"]} == {lexical_id, semantic_id}
    assert failed["can_retry_summary"] is True
    assert "work_json" not in failed
    runtime = SmartSearchRuntime(AppSettings(), SimpleNamespace(pause_controller=SimpleNamespace(is_paused=lambda: False)))
    runtime.retry_summary(repository, job_id)
    resumed = repository.get_smart_search_job(job_id)
    assert resumed["status"] == "running"
    assert resumed["work_json"]["reference_time"] == work["reference_time"]
    assert resumed["matching_sources_json"] == failed["matching_sources"]
    tasks = list(repository._execute("SELECT kind FROM work_queue WHERE payload->>'search_id'=%s", (str(job_id),)))
    assert [task["kind"] for task in tasks] == ["search_answer"]
    repository.cancel_smart_search_job(job_id)
    assert len(_public_job(repository.get_smart_search_job(job_id))["matching_sources"]) == 2
    repository.connection.close()


def test_postgres_repository_full_text_and_job_progress():
    repository = build_repository()
    account_id = repository.upsert_mail_account(
        MailAccount(id=None, name="Inbox", provider_type="imap", auth_mode="env", secret_locator="MAIL_PASSWORD", settings={})
    )
    mailbox_id = repository.upsert_mailbox(Mailbox(id=None, account_id=account_id, provider_mailbox_id="INBOX", name="Inbox"))
    message_id = repository.upsert_message(
        mailbox_id,
        NormalizedMessage(
            provider_message_id="m1",
            mailbox_provider_id="INBOX",
            internet_message_id="<m1@example.com>",
            thread_id="t1",
            subject="Denver trip confirmation",
            sender_name="Travel Desk",
            sender_address="travel@example.com",
            recipients=["me@example.com"],
            cc=[],
            received_at=datetime(2026, 4, 1, tzinfo=UTC),
            sent_at=datetime(2026, 4, 1, tzinfo=UTC),
            unread=True,
            has_attachments=False,
            importance_hint=None,
            snippet_hash="hash",
        ),
        "<html><style>.x{}</style>Denver hotel booking and flight details</html>",
        [],
    )
    repository.upsert_summary(
        MessageSummary(
            message_id=message_id,
            summary_text="Your Denver travel booking has hotel and flight details.",
            importance_score=0.9,
            rationale="travel booking",
            confidence=0.8,
            model_name="test",
        )
    )

    assert repository.search_messages("Where am I traveling next?", limit=5)[0]["id"] == message_id

    job_id = repository.create_job(JobRecord(id=None, job_type="sync"))
    repository.update_job(job_id, phase="embedding", total_count=1, processed_count=1, current_item="Denver trip")
    job = repository.get_job(job_id)
    assert job["phase"] == "embedding"
    assert job["processed_count"] == 1

    sync_lock_job_id = repository.create_job(JobRecord(id=None, job_type="sync", metadata={"account_id": account_id}))
    sync_lock_job = repository.get_running_sync_job_for_account(account_id)
    assert sync_lock_job["id"] == sync_lock_job_id

    reset_result = repository.reset_processed_messages_for_accounts([account_id])
    assert reset_result["deleted_messages"] == 1
    assert repository.get_message_detail(message_id) is None


def test_postgres_interest_profile_upsert_generates_and_updates_id():
    repository = build_repository()

    profile_id = repository.upsert_interest_profile(
        InterestProfile(
            id=None,
            name="Travel",
            description="Travel bookings",
            collaborators=["travel@example.com"],
            signals="flight hotel booking",
        )
    )
    updated_id = repository.upsert_interest_profile(
        InterestProfile(
            id=profile_id,
            name="Travel renamed",
            description="Travel bookings and reservations",
            collaborators=[],
            signals="flight hotel booking reservation",
        )
    )

    profile = repository.list_interest_profiles()[0]
    assert profile_id > 0
    assert updated_id == profile_id
    assert profile["id"] == profile_id
    assert profile["name"] == "Travel renamed"


def test_postgres_llm_preset_round_trip_text_context_window():
    repository = build_repository()
    message_id = seed_ready_embedding(repository, provider_message_id='preset-save-vector',
        subject='Preserve existing vector', embedding_model='canonical', vector=[1.0, 0.0])

    preset_id, _ = repository.upsert_llm_preset(
        LLMPreset(
            id=None,
            name="Context Window Test",
            provider="ollama",
            enabled=True,
            priority=5,
            endpoint_url="http://127.0.0.1:11434",
            text_model="chat",
            embedding_model="embed",
            text_context_window=4096,
            embedding_context_window=2048,
            max_output_tokens=700,
            initial_timeout_seconds=90,
            streaming_timeout_seconds=30,
        )
    )
    repository.upsert_llm_preset(
        LLMPreset(
            id=preset_id,
            name="Context Window Test",
            provider="ollama",
            enabled=True,
            priority=5,
            endpoint_url="http://127.0.0.1:11434",
            text_model="chat",
            embedding_model="embed",
            text_context_window=6144,
            embedding_context_window=3072,
            max_output_tokens=700,
            initial_timeout_seconds=120,
            streaming_timeout_seconds=45,
        )
    )

    preset = next(item for item in repository.list_llm_presets() if item["name"] == "Context Window Test")

    assert preset["id"] == preset_id
    assert preset["text_context_window"] == 6144
    assert preset["embedding_context_window"] == 3072
    assert preset["initial_timeout_seconds"] == 120
    assert preset["streaming_timeout_seconds"] == 45
    assert repository._execute("SELECT status FROM message_embeddings WHERE message_id=%s", (message_id,)).fetchone()['status'] == 'ready'
    repository.upsert_llm_preset(LLMPreset(id=None, name='Language only', embedding_model=''))
    assert repository._execute("SELECT status FROM message_embeddings WHERE message_id=%s", (message_id,)).fetchone()['status'] == 'ready'

    repository._execute("UPDATE message_embeddings SET status='stale' WHERE message_id=%s", (message_id,))
    repository._execute("UPDATE message_processing_state SET embedding_needs_refresh=true WHERE message_id=%s", (message_id,))
    assert repository.restore_current_message_embeddings('canonical') == 0
    repository._execute("UPDATE message_processing_state SET embedding_needs_refresh=false WHERE message_id=%s", (message_id,))
    assert repository.restore_current_message_embeddings('canonical') == 1
    repository._execute("UPDATE message_embeddings SET status='stale', input_hash='changed' WHERE message_id=%s", (message_id,))
    assert repository.restore_current_message_embeddings('canonical') == 0


def test_interval_summary_stale_queries_return_newly_inserted_rows():
    repository = build_repository()
    message_id = seed_message(repository, provider_message_id="interval-one-pass", subject="One-pass interval summary")
    repository.upsert_summary(
        MessageSummary(
            message_id=message_id,
            summary_text="A useful message summary.",
            importance_score=0.7,
            rationale="test seed",
            confidence=0.8,
            model_name="test",
        )
    )

    daily_rows = repository.list_stale_daily_interval_summaries("UTC", limit=10)

    assert len(daily_rows) == 1
    assert daily_rows[0]["summary_type"] == "daily"
    assert daily_rows[0]["period_days"] == 1

    repository.mark_interval_summary_fresh(
        daily_rows[0]["id"],
        {"entries": [{"summary": "Daily summary text.", "message_ids": [message_id]}]},
    )

    rollup_rows = repository.list_stale_rollup_interval_summaries(limit=10)

    assert {(row["summary_type"], row["period_days"]) for row in rollup_rows} == {
        ("weekly", 7),
        ("monthly", 30),
    }


def test_interval_summary_merge_fallback_remains_retryable_and_is_replaced():
    repository = build_repository()
    message_id = seed_message(repository, provider_message_id="interval-merge-fallback", subject="Merge fallback seed")
    repository.upsert_summary(
        MessageSummary(
            message_id=message_id,
            summary_text="A useful message summary.",
            importance_score=0.7,
            rationale="test seed",
            confidence=0.8,
            model_name="test",
        )
    )
    daily_row = repository.list_stale_daily_interval_summaries("UTC", limit=10)[0]
    repository.mark_interval_summary_fresh(
        daily_row["id"],
        {"entries": [{"summary": "Daily summary text.", "message_ids": [message_id]}]},
    )
    monthly_row = next(row for row in repository.list_stale_rollup_interval_summaries(limit=10) if row["summary_type"] == "monthly")
    fallback_json = {
        "entries": [
            {"summary": "Unmerged batch summary.", "message_ids": [message_id]},
            {
                "summary": "Automatic merge of batched summaries failed; showing unmerged batch summaries until retry succeeds.",
                "message_ids": [],
            },
        ]
    }

    repository.mark_interval_summary_merge_fallback(monthly_row["id"], fallback_json)

    fallback_rows = repository.list_interval_summaries([(monthly_row["interval_start_date"], monthly_row["period_days"])])
    fallback_row = next(row for row in fallback_rows if row["id"] == monthly_row["id"])
    assert fallback_row["refreshed_at"] is None
    assert fallback_row["summary_json"] == fallback_json
    search_rows = repository.search_interval_summaries("Unmerged batch summary", limit=5)
    assert monthly_row["id"] not in {row["id"] for row in search_rows}
    assert daily_row["id"] in {row["id"] for row in search_rows}
    stale_rows = repository.list_stale_rollup_interval_summaries(limit=10)
    assert any(row["id"] == monthly_row["id"] for row in stale_rows)

    fresh_json = {"entries": [{"summary": "Merged monthly summary.", "message_ids": [message_id]}]}
    repository.mark_interval_summary_fresh(monthly_row["id"], fresh_json)

    refreshed_rows = repository.list_interval_summaries([(monthly_row["interval_start_date"], monthly_row["period_days"])])
    refreshed_row = next(row for row in refreshed_rows if row["id"] == monthly_row["id"])
    assert refreshed_row["refreshed_at"] is not None
    assert refreshed_row["summary_json"] == fresh_json


def test_interval_summary_search_documents_and_embeddings():
    repository = build_repository()
    message_id = seed_message(repository, provider_message_id="interval-search", subject="Interval search seed")
    repository.upsert_summary(
        MessageSummary(
            message_id=message_id,
            summary_text="Security review and launch follow-up were discussed.",
            importance_score=0.7,
            rationale="test seed",
            confidence=0.8,
            model_name="test",
        )
    )
    daily_row = repository.list_stale_daily_interval_summaries("UTC", limit=10)[0]
    summary_id = int(daily_row["id"])

    repository.mark_interval_summary_fresh(
        summary_id,
        {"entries": [{"summary": "Daily launch review summary mentions security approval.", "message_ids": [message_id]}]},
    )

    lexical = repository.search_interval_summaries("security approval", limit=5)
    assert lexical[0]["id"] == summary_id

    detail = repository.get_interval_summary_embedding_detail(summary_id)
    input_text = repository.interval_summary_embedding_input_for_row(detail)
    repository.upsert_interval_summary_embedding(
        summary_id=summary_id,
        preset_name="test",
        embedding_model="embed",
        input_text=input_text,
        vector=[0.1, 0.2, 0.3],
    )
    repository.complete_interval_summary_embedding(
        summary_id,
        input_text=input_text,
        embedding_model="embed",
        preset_name="test",
    )

    vector_rows = repository.search_interval_summaries_by_embedding([0.1, 0.2, 0.3], "embed", limit=5)
    assert vector_rows[0]["id"] == summary_id

    repository.mark_interval_summary_fresh(
        summary_id,
        {"entries": [{"summary": "Updated daily summary mentions urgent travel.", "message_ids": [message_id]}]},
    )
    refreshed = repository.get_interval_summary_embedding_detail(summary_id)
    assert refreshed["embedding_status"] == "pending"
    assert refreshed["embedding_needs_refresh"] is True
    assert repository.search_interval_summaries("urgent travel", limit=5)[0]["id"] == summary_id






def test_incremental_clustering_queue_recovers_and_commits_each_message():
    from types import SimpleNamespace
    from mailintel.services import cluster_queue
    repository = build_repository()
    embedding_model = "incremental-test-model"
    message_ids = {
        seed_ready_embedding(
            repository,
            provider_message_id="cluster-one",
            subject="Security incident follow-up",
            embedding_model=embedding_model,
            vector=[1.0, 0.0, 0.0],
        ),
        seed_ready_embedding(
            repository,
            provider_message_id="cluster-two",
            subject="Security incident status",
            embedding_model=embedding_model,
            vector=[0.99, 0.01, 0.0],
        ),
    }

    counts = repository.initialize_incremental_clustering(
        embedding_model,
        INCREMENTAL_CLUSTERING_ALGORITHM_VERSION,
    )
    assert counts == {"todo": 2, "in_progress": 0, "done": 0}

    cluster_queue.discover(repository, embedding_model)
    initially_claimed = repository.queue_claim("local", None, ["cluster_message"])
    assert initially_claimed is not None
    assert repository.queue_recover_processing() == 1

    handlers = {}
    runtime = SimpleNamespace(register=lambda kind, handler, **_kwargs: handlers.update({kind: handler}))
    llm = SimpleNamespace(label_topic_cluster=lambda _preset, **kwargs: replace(
        kwargs["fallback"], model_name="test:topic-label",
    ))
    cluster_queue.register(runtime, llm)

    def drain_clustering():
        while True:
            job = repository.queue_claim("test-worker", None, list(handlers))
            if job is None:
                break
            apply = handlers[job["kind"]](repository, job, None)
            repository.queue_complete(job, apply=apply)

    drain_clustering()

    assert repository.get_incremental_clustering_work_counts(
        embedding_model,
        INCREMENTAL_CLUSTERING_ALGORITHM_VERSION,
    ) == {"todo": 0, "in_progress": 0, "done": 2}
    assert repository.get_clustering_status_counts(embedding_model) == {
        "ready_embeddings": 2,
        "clustered_messages": 2,
    }
    memberships = list(
        repository.connection.execute(
            "SELECT message_id, topic_cluster_id FROM message_topic_memberships ORDER BY message_id"
        )
    )
    assert {int(row["message_id"]) for row in memberships} == message_ids
    assert len({int(row["topic_cluster_id"]) for row in memberships}) == 1

    changed_message_id = max(message_ids)
    changed_input = "changed clustering input"
    repository.upsert_message_embedding(
        message_id=changed_message_id,
        preset_name="test",
        embedding_model=embedding_model,
        input_text=changed_input,
        vector=[0.0, 1.0, 0.0],
    )
    repository.complete_embedding(
        changed_message_id,
        input_text=changed_input,
        embedding_model=embedding_model,
        preset_name="test",
    )
    assert repository.get_clustering_status_counts(embedding_model) == {
        "ready_embeddings": 2,
        "clustered_messages": 1,
    }

    cluster_queue.enqueue(repository, changed_message_id, embedding_model,
                          repository.embedding_input_hash(changed_input))
    drain_clustering()
    assert repository.get_clustering_status_counts(embedding_model) == {
        "ready_embeddings": 2,
        "clustered_messages": 2,
    }
    memberships = list(repository.connection.execute("SELECT topic_cluster_id FROM message_topic_memberships"))
    assert len({int(row["topic_cluster_id"]) for row in memberships}) == 2


def test_legacy_preset_backup_import_preserves_model_routing():
    from mailintel.config.backup import _preset_import_rows

    rows = _preset_import_rows([
        {"name": "Legacy", "chat_model": "chat", "summary_model": "summary", "context_window": 16384,
         "embedding_model": "embed", "concurrency": 4,
         "capabilities_json": ["mail_summary", "search_answer", "mail_embedding"]},
        {"name": "Legacy [summaries]", "text_model": "already-modern", "text_context_window": 32000,
         "embedding_context_window": 2048},
        {"name": "No summaries", "chat_model": "chat", "summary_model": "", "capabilities_json": ["mail_summary", "search_answer"]},
    ])
    by_name = {row["name"]: row for row in rows}
    assert by_name["Legacy"]["capabilities_json"] == ["search_answer", "mail_embedding"]
    assert by_name["Legacy [summaries 1]"]["text_model"] == "summary"
    assert by_name["Legacy [summaries 1]"]["embedding_model"] == ""
    assert by_name["Legacy"]["concurrency"] + by_name["Legacy [summaries 1]"]["concurrency"] == 4
    assert by_name["Legacy"]["embedding_context_window"] == 16384
    assert by_name["Legacy [summaries]"]["embedding_context_window"] == 2048
    assert by_name["No summaries"]["capabilities_json"] == ["search_answer"]


def test_queue_controls_preserve_pause_priority_and_revoke_processing_ownership():
    repository = build_repository()
    job = repository.queue_enqueue('interval_daily', 'controlled', {'phase': 'first'}, priority=2)
    claimed = repository.queue_claim('worker-a', None, ['interval_daily'])
    assert repository.queue_is_owned(claimed)
    assert repository.queue_update_priority(job['id'], 7)['priority'] == 7
    paused = repository.queue_pause(job['id'])
    assert paused['state'] == 'paused' and paused['retries'] == 0
    assert not repository.queue_is_owned(claimed)
    applied = []
    assert not repository.queue_complete(claimed, lambda: applied.append(True))
    assert not repository.queue_requeue(claimed, 'late failure')
    assert applied == []
    repository.queue_enqueue('interval_daily', 'controlled', {'phase': 'updated'}, priority=2)
    assert repository.queue_recover_processing() == 0
    listing = repository.queue_list(state='paused')
    assert listing['total'] == 1 and listing['jobs'][0]['priority'] == 7
    assert 'payload' not in listing['jobs'][0]
    assert repository.queue_claim('worker-b', None, ['interval_daily']) is None
    resumed = repository.queue_resume(job['id'])
    assert resumed['state'] == 'new' and resumed['retries'] == 0
    claimed_again = repository.queue_claim('worker-b', None, ['interval_daily'])
    repository.queue_update_priority(job['id'], 9)
    assert repository.queue_complete(claimed_again, lambda: repository.queue_enqueue(
        'interval_daily', 'controlled', {'phase': 'continuation'}, priority=claimed_again['priority']))
    assert repository.queue_list()['jobs'][0]['priority'] == 9
    next_job = repository.queue_claim('worker-c', None, ['interval_daily'])
    assert repository.queue_delete(next_job['id'])['state'] == 'processing'
    assert not repository.queue_complete(next_job, lambda: applied.append(True))
    assert repository.queue_list()['total'] == 0
    assert repository.queue_pause(next_job['id']) is None
    actions = [event['action'] for event in repository.queue_events()]
    assert all(action in actions for action in ('paused', 'resumed', 'reprioritized', 'deleted'))
    repository.connection.close()


def test_global_pause_requeues_only_preset_claims_and_invalidates_late_results():
    repository = build_repository()
    preset_id, _ = repository.upsert_llm_preset(LLMPreset(name='Pause verification'))
    analysis = repository.queue_enqueue('analyze_message', 'pause-analysis', {'step': 'current'}, priority=2)
    repository.queue_enqueue('embed_message', 'pause-embedding', {}, priority=2)
    held = repository.queue_enqueue('interval_daily', 'pause-held', {}, priority=3)
    repository.queue_pause(held['id'])
    repository.queue_enqueue('cluster_message', 'pause-local', {}, priority=5)
    old = repository.queue_claim('preset-worker', preset_id, ['analyze_message'])
    repository.queue_claim('embedding-worker', preset_id, ['embed_message'])
    local = repository.queue_claim('local', None, ['cluster_message'])
    assert repository.queue_interrupt_presets('Interrupted by global AI pause') == 2
    rows = {row['dedup_key']: row for row in repository.queue_list()['jobs']}
    assert rows['pause-analysis']['state'] == rows['pause-embedding']['state'] == 'new'
    assert rows['pause-analysis']['retries'] == rows['pause-embedding']['retries'] == 1
    assert rows['pause-held']['state'] == 'paused' and rows['pause-held']['retries'] == 0
    assert rows['pause-local']['state'] == 'processing'
    fresh = repository.queue_claim('preset-worker', preset_id, ['analyze_message'])
    assert fresh['id'] == analysis['id'] and fresh['payload'] == {'step': 'current'}
    assert fresh['claimed_revision'] > old['claimed_revision']
    assert not repository.queue_complete(old, lambda: pytest.fail('Interrupted result must not publish'))
    assert not repository.queue_requeue(old, 'Late error')
    assert repository.queue_complete(fresh)
    assert repository.queue_complete(local)
    events = [row for row in repository.queue_events() if row['action'] == 'requeued']
    assert len(events) == 2 and all(row['reason'] == 'Interrupted by global AI pause' for row in events)
    repository.connection.close()


def test_queue_preview_search_filters_and_pagination_hide_full_payloads():
    repository = build_repository()
    message_id = seed_message(repository, provider_message_id='preview-mail', subject='Renewal for customer 50%')
    repository._execute("UPDATE messages SET cleaned_text=%s, search_document=to_tsvector('simple',%s) WHERE id=%s",
                        ('Distinctive zeppelin excerpt ' + 'x' * 1000, 'Distinctive zeppelin excerpt', message_id))
    mailbox_id = repository._execute('SELECT mailbox_id FROM messages WHERE id=%s', (message_id,)).fetchone()['mailbox_id']
    summary_id = repository._execute("""INSERT INTO mailbox_interval_summaries
        (mailbox_id,summary_type,interval_start_date,period_days) VALUES(%s,'weekly','2026-10-05',7) RETURNING id""",
        (mailbox_id,)).fetchone()['id']
    repository.connection.commit()
    first = repository.queue_enqueue('analyze_message', 'mail-preview', {'message_id': message_id, 'secret': 'hidden'}, priority=2)
    second = repository.queue_enqueue('embed_message', 'embedding-preview', {'message_id': message_id}, priority=2)
    interval = repository.queue_enqueue('interval_rollup', 'weekly-preview', {'summary_id': summary_id}, priority=4)
    orphan = repository.queue_enqueue('relabel_topic', 'deleted-topic-preview', {'topic_id': 'not-an-id'}, priority=5)
    repository.queue_pause(second['id'])
    page = repository.queue_list(limit=1)
    assert page['total'] == 4 and len(page['jobs']) == 1
    assert 'Renewal for customer 50%' in page['jobs'][0]['preview']
    assert 'sender@example.com' in page['jobs'][0]['preview']
    assert 'Distinctive zeppelin excerpt' in page['jobs'][0]['preview']
    assert len(page['jobs'][0]['preview']) == 350 and 'payload' not in page['jobs'][0]
    assert repository.queue_list(q='zeppelin')['total'] == 2
    assert repository.queue_list(q='50%')['total'] == 2
    assert repository.queue_list(q='50_')['total'] == 0
    filtered = repository.queue_list(q='zeppelin', state='paused', kind='embed_message')
    assert [row['id'] for row in filtered['jobs']] == [second['id']]
    assert filtered['kinds'] == ['analyze_message', 'embed_message', 'interval_rollup', 'relabel_topic']
    interval_page = repository.queue_list(q='2026-10-05', kind='interval_rollup')
    assert [row['id'] for row in interval_page['jobs']] == [interval['id']]
    assert interval_page['jobs'][0]['preview'] == 'Inbox · weekly · 2026-10-05 · 7 days'
    assert repository.queue_list(q='deleted-topic')['jobs'][0]['preview'] == 'deleted-topic-preview'
    assert repository.queue_list(limit=1, offset=3)['jobs'][0]['id'] == orphan['id']
    assert repository.queue_list(limit=1, offset=10)['total'] == 4
    repository.connection.close()


def test_explicit_rejection_removes_preview_and_prevents_late_pass_resurrection():
    repository = build_repository()
    job_id = repository.create_job(JobRecord(id=None, job_type='smart_search'))
    repository._execute("INSERT INTO smart_search_jobs (job_id,question,status) VALUES (%s,'event','running')", (job_id,))
    sources = [{'id': i, 'keyword_match_count': i} for i in range(1, 4)]
    repository.merge_smart_search_matches(job_id, sources)
    repository.reject_smart_search_matches(job_id, [2])
    repository.merge_smart_search_matches(job_id, sources)
    job = repository.get_smart_search_job(job_id)
    assert [row['id'] for row in job['matching_sources_json']] == [3, 1]
    assert job['rejected_message_ids_json'] == [2]
    repository.connection.close()


def test_message_stars_persist_and_survive_sync():
    repository = build_repository()
    first = seed_message(repository, provider_message_id="star-first")
    second = seed_message(repository, provider_message_id="star-second")
    assert repository.get_message_detail(first)["star"] == 0
    assert repository.list_message_stars() == []
    assert repository.set_message_star(first, 1) == {"id": first, "star": 1}
    repository.set_message_star(second, 3)
    repository.connection.rollback()  # Saving a star must already have committed.
    assert repository.get_message_detail(second)["star"] == 3
    assert [row["id"] for row in repository.list_starred_messages(1)] == [second]
    assert [row["id"] for row in repository.list_starred_messages(1, 1)] == [first]
    seed_message(repository, provider_message_id="star-first", subject="Updated subject")
    assert repository.get_message_detail(first)["star"] == 1
    repository.set_message_star(first, 0)
    assert repository.list_message_stars() == [{"id": second, "star": 3}]
    assert repository.set_message_star(999999999, 1) is None
    assert repository.connection.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND indexname = 'idx_messages_star'").fetchone()


def test_initial_migration_matches_initializer_and_preserves_data_on_repeat():
    from pathlib import Path

    repository = build_repository()
    connection = repository.connection
    expected_schema = connection.execute('SELECT current_schema() AS name').fetchone()['name']

    def describe(schema):
        columns = list(connection.execute('''
            SELECT table_name, column_name, data_type, is_nullable, column_default,
                   is_identity, identity_generation, generation_expression
            FROM information_schema.columns WHERE table_schema=%s
            ORDER BY table_name, ordinal_position
        ''', (schema,)))
        indexes = list(connection.execute('''
            SELECT tablename, indexname, indexdef FROM pg_indexes
            WHERE schemaname=%s ORDER BY tablename, indexname
        ''', (schema,)))
        constraints = list(connection.execute('''
            SELECT r.relname, c.conname, pg_get_constraintdef(c.oid) AS definition
            FROM pg_constraint c JOIN pg_class r ON r.oid=c.conrelid
            JOIN pg_namespace n ON n.oid=r.relnamespace
            WHERE n.nspname=%s ORDER BY r.relname, c.conname
        ''', (schema,)))
        return json.dumps([columns, indexes, constraints], sort_keys=True).replace(schema + '.', '')

    expected = describe(expected_schema)
    migration_schema = f'test_{uuid.uuid4().hex}'
    connection.execute(f'CREATE SCHEMA {migration_schema}')
    connection.execute(f'SET search_path TO {migration_schema}, public')
    sql = (Path(__file__).parents[2] / 'backend/mailintel/persistence/migrations/versions/0001_initial_schema.sql').read_text()
    connection.execute(sql)
    assert describe(migration_schema) == expected
    connection.execute("UPDATE llm_presets SET notes='preserve customized settings'")
    connection.commit()
    init_database(connection)
    assert connection.execute('SELECT notes FROM llm_presets').fetchone()['notes'] == 'preserve customized settings'
    assert describe(migration_schema) == expected
    connection.close()
