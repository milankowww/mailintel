# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from mailintel.persistence.orm import SPECIAL_DDL, baseline_statements, metadata


def test_orm_baseline_covers_current_schema_tables() -> None:
    expected_tables = {
        "mail_accounts",
        "mailboxes",
        "user_profile",
        "llm_presets",
        "pipeline_settings",
        "interest_profiles",
        "messages",
        "message_summaries",
        "message_processing_state",
        "message_body_cache",
        "message_embeddings",
        "llm_preset_stage_health",
        "cluster_rebuild_state",
        "message_clustering_work",
        "topic_clusters",
        "message_interest_matches",
        "message_topic_memberships",
        "mailbox_interval_summaries",
        "interval_summary_embeddings",
        "attachments",
        "jobs",
        "smart_search_jobs",
        "smart_search_job_messages",
    }

    assert set(metadata.tables) == expected_tables


def test_orm_baseline_keeps_postgres_specific_ddl_centralized() -> None:
    statements = baseline_statements()
    joined = "\n".join(statements)

    assert statements[0] == "CREATE EXTENSION IF NOT EXISTS vector"
    assert "CREATE TABLE IF NOT EXISTS mail_accounts" in joined
    assert any("CREATE INDEX IF NOT EXISTS idx_messages_search_document" in statement for statement in SPECIAL_DDL)
    assert "USING hnsw" in joined
    assert "INSERT INTO pipeline_settings" in joined
    assert "INSERT INTO llm_presets" in joined
    assert "WHERE NOT EXISTS (SELECT 1 FROM llm_presets)" in joined


def test_baseline_creates_final_schema_without_historical_transforms() -> None:
    statements = baseline_statements()
    assert not any(s.strip().startswith(("ALTER ", "DROP ", "UPDATE ", "DO ")) for s in statements)
    joined = "\n".join(statements)
    assert "work_queue_state_priority_id" in joined
    assert "'paused'" in joined
    assert "matching_sources_json" in joined
    assert "streaming_timeout_seconds" in joined
