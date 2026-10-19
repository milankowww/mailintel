from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.schema import CreateIndex, CreateTable
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType):
    cache_ok = True

    def get_col_spec(self, **_kw) -> str:
        return "vector"


metadata = MetaData()

mail_accounts = Table(
    "mail_accounts",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", Text, nullable=False),
    Column("provider_type", Text, nullable=False),
    Column("auth_mode", Text, nullable=False),
    Column("secret_locator", Text),
    Column("encrypted_password", Text),
    Column("password_encryption", Text),
    Column("settings_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

mailboxes = Table(
    "mailboxes",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("account_id", BigInteger, ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False),
    Column("provider_mailbox_id", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("monitored", Boolean, nullable=False, server_default=text("true")),
    Column("sync_cursor", Text),
    Column("last_synced_at", DateTime(timezone=True)),
    Column("crawl_status", Text, nullable=False, server_default=text("'idle'")),
    Column("crawl_lease_expires_at", DateTime(timezone=True)),
    Column("crawl_attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("crawl_last_error", Text, nullable=False, server_default=text("''")),
    Column("next_sync_at", DateTime(timezone=True)),
    Column("last_crawled_at", DateTime(timezone=True)),
    UniqueConstraint("account_id", "provider_mailbox_id"),
)

user_profile = Table(
    "user_profile",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column("name", Text, nullable=False, server_default=text("''")),
    Column("role", Text, nullable=False, server_default=text("''")),
    Column("organization", Text, nullable=False, server_default=text("''")),
    Column("aliases_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("email_addresses_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("collaborators_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("communication_preferences", Text, nullable=False, server_default=text("''")),
    Column("about", Text, nullable=False, server_default=text("''")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("id = 1"),
)

llm_presets = Table(
    "llm_presets",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("provider", Text, nullable=False, server_default=text("'ollama'")),
    Column("enabled", Boolean, nullable=False, server_default=text("false")),
    Column("priority", Integer, nullable=False, server_default=text("100")),
    Column("concurrency", Integer, nullable=False, server_default=text("1")),
    Column("capabilities_json", JSONB, nullable=False, server_default=text('\'["mail_summary","mail_embedding","daily_summary","rollup_summary","topic_label","interval_embedding","search_plan","search_embedding","search_compress","search_answer"]\'::jsonb')),
    CheckConstraint("concurrency BETWEEN 1 AND 20"),
    Column("auth_mode", Text, nullable=False, server_default=text("'env'")),
    Column("secret_locator", Text, nullable=False, server_default=text("''")),
    Column("endpoint_url", Text, nullable=False, server_default=text("'http://127.0.0.1:11434'")),
    Column("text_model", Text, nullable=False, server_default=text("'llama3.1'")),
    Column("embedding_model", Text, nullable=False, server_default=text("'nomic-embed-text'")),
    Column("temperature", Float, nullable=False, server_default=text("0.2")),
    Column("text_context_window", Integer, nullable=False, server_default=text("8192")),
    Column("embedding_context_window", Integer, nullable=False, server_default=text("8192")),
    Column("max_output_tokens", Integer, nullable=False, server_default=text("800")),
    Column("initial_timeout_seconds", Float),
    Column("streaming_timeout_seconds", Float),
    Column("notes", Text, nullable=False, server_default=text("''")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

pipeline_settings = Table(
    "pipeline_settings",
    metadata,
    Column("id", SmallInteger, primary_key=True, autoincrement=False),
    Column("analysis_mode", Text, nullable=False, server_default=text("'priority'")),
    Column("embedding_mode", Text, nullable=False, server_default=text("'priority'")),
    Column("canonical_embedding_model", Text, nullable=False, server_default=text("''")),
    Column("search_planner_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("search_embedding_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("search_compressor_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("search_answer_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    CheckConstraint("id = 1"),
)

interest_profiles = Table(
    "interest_profiles",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("description", Text, nullable=False),
    Column("collaborators_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("signals", Text, nullable=False, server_default=text("''")),
    Column("priority_weight", Float, nullable=False, server_default=text("1.0")),
    Column("dashboard_prominence", Integer, nullable=False, server_default=text("1")),
    Column("summary_detail", Text, nullable=False, server_default=text("'standard'")),
    Column("watch_mode", Text, nullable=False, server_default=text("'watch'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

messages = Table(
    "messages",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("mailbox_id", BigInteger, ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False),
    Column("provider_message_id", Text, nullable=False),
    Column("internet_message_id", Text),
    Column("thread_id", Text),
    Column("subject", Text, nullable=False),
    Column("sender_name", Text, nullable=False, server_default=text("''")),
    Column("sender_address", Text, nullable=False, server_default=text("''")),
    Column("recipients_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("cc_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("received_at", DateTime(timezone=True)),
    Column("sent_at", DateTime(timezone=True)),
    Column("star", Integer, nullable=False, server_default=text("0")),
    Column("is_read", Boolean, nullable=False, server_default=text("false")),
    Column("has_attachments", Boolean, nullable=False, server_default=text("false")),
    Column("importance_hint", Text),
    Column("snippet_hash", Text, nullable=False),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("cleaned_text", Text, nullable=False, server_default=text("''")),
    Column("search_document", TSVECTOR, nullable=False, server_default=text("''::tsvector")),
    Column("fetched_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("mailbox_id", "provider_message_id"),
)

message_summaries = Table(
    "message_summaries",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True),
    Column("summary_text", Text, nullable=False),
    Column("importance_score", Float, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("summarized_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

message_processing_state = Table(
    "message_processing_state",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True),
    Column("source_input_hash", Text, nullable=False, server_default=text("''")),
    Column("interest_status", Text, nullable=False, server_default=text("'pending'")),
    Column("interest_input_hash", Text, nullable=False, server_default=text("''")),
    Column("summary_status", Text, nullable=False, server_default=text("'pending'")),
    Column("summary_input_hash", Text, nullable=False, server_default=text("''")),
    Column("embedding_status", Text, nullable=False, server_default=text("'pending'")),
    Column("embedding_input_hash", Text, nullable=False, server_default=text("''")),
    Column("embedding_model", Text, nullable=False, server_default=text("''")),
    Column("interest_lease_expires_at", DateTime(timezone=True)),
    Column("summary_lease_expires_at", DateTime(timezone=True)),
    Column("embedding_lease_expires_at", DateTime(timezone=True)),
    Column("interest_attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("summary_attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("embedding_attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("interest_last_error", Text, nullable=False, server_default=text("''")),
    Column("summary_last_error", Text, nullable=False, server_default=text("''")),
    Column("embedding_last_error", Text, nullable=False, server_default=text("''")),
    Column("summary_needs_refresh", Boolean, nullable=False, server_default=text("false")),
    Column("embedding_needs_refresh", Boolean, nullable=False, server_default=text("false")),
    Column("last_summary_preset_name", Text, nullable=False, server_default=text("''")),
    Column("last_embedding_preset_name", Text, nullable=False, server_default=text("''")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

message_body_cache = Table(
    "message_body_cache",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True),
    Column("body_text", Text, nullable=False),
    Column("source_segment", Text, nullable=False, server_default=text("'body'")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

message_embeddings = Table(
    "message_embeddings",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
    Column("preset_name", Text, nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("input_hash", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'ready'")),
    Column("embedding", Vector),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("message_id", "embedding_model"),
)

llm_preset_stage_health = Table(
    "llm_preset_stage_health",
    metadata,
    Column("preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="CASCADE"), nullable=False),
    Column("stage", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'healthy'")),
    Column("consecutive_failures", Integer, nullable=False, server_default=text("0")),
    Column("last_failure_at", DateTime(timezone=True)),
    Column("last_success_at", DateTime(timezone=True)),
    Column("cooldown_until", DateTime(timezone=True)),
    Column("last_error_text", Text, nullable=False, server_default=text("''")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("preset_id", "stage"),
)

cluster_rebuild_state = Table(
    "cluster_rebuild_state",
    metadata,
    Column("embedding_model", Text, primary_key=True),
    Column("status", Text, nullable=False, server_default=text("'idle'")),
    Column("dirty_since", DateTime(timezone=True)),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error_text", Text, nullable=False, server_default=text("''")),
    Column("last_completed_at", DateTime(timezone=True)),
    Column("last_ready_embedding_count", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

message_clustering_work = Table(
    "message_clustering_work",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("algorithm_version", Text, nullable=False),
    Column("source_hash", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'todo'")),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("retry_at", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error_text", Text, nullable=False, server_default=text("''")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("message_id", "embedding_model"),
    CheckConstraint("status IN ('todo', 'in_progress', 'done')"),
)

topic_clusters = Table(
    "topic_clusters",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("topic_key", Text, nullable=False, unique=True),
    Column("embedding_model", Text, nullable=False, server_default=text("''")),
    Column("name", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("key_terms_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("key_entities_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("llm_summary", Text, nullable=False, server_default=text("''")),
    Column("why_it_matters", Text, nullable=False, server_default=text("''")),
    Column("importance_score", Float, nullable=False, server_default=text("0.0")),
    Column("model_name", Text, nullable=False, server_default=text("''")),
    Column("centroid", Vector),
    Column("dimensions", Integer, Computed("vector_dims(centroid)", persisted=True)),
    Column("frontier_at", DateTime(timezone=True)),
    Column("algorithm_version", Text, nullable=False, server_default=text("''")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
)

message_interest_matches = Table(
    "message_interest_matches",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
    Column("interest_profile_id", BigInteger, ForeignKey("interest_profiles.id", ondelete="CASCADE"), nullable=False),
    Column("score", Float, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("primary_match", Boolean, nullable=False, server_default=text("false")),
    Column("assigned_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("message_id", "interest_profile_id"),
)

message_topic_memberships = Table(
    "message_topic_memberships",
    metadata,
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
    Column("topic_cluster_id", BigInteger, ForeignKey("topic_clusters.id", ondelete="CASCADE"), nullable=False),
    Column("score", Float, nullable=False),
    Column("is_primary", Boolean, nullable=False, server_default=text("false")),
    Column("assigned_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("message_id", "topic_cluster_id"),
)

mailbox_interval_summaries = Table(
    "mailbox_interval_summaries",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("mailbox_id", BigInteger, ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False),
    Column("summary_type", Text, nullable=False),
    Column("interval_start_date", Date, nullable=False),
    Column("period_days", Integer, nullable=False),
    Column("refreshed_at", DateTime(timezone=True)),
    Column("request_rebuild", DateTime(timezone=True)),
    Column("summary_json", JSONB, nullable=False, server_default=text('\'{\"entries\":[]}\'::jsonb')),
    Column("search_document", TSVECTOR, nullable=False, server_default=text("''::tsvector")),
    Column("embedding_status", Text, nullable=False, server_default=text("'pending'")),
    Column("embedding_input_hash", Text, nullable=False, server_default=text("''")),
    Column("embedding_model", Text, nullable=False, server_default=text("''")),
    Column("embedding_lease_expires_at", DateTime(timezone=True)),
    Column("embedding_attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("embedding_last_error", Text, nullable=False, server_default=text("''")),
    Column("embedding_needs_refresh", Boolean, nullable=False, server_default=text("false")),
    Column("last_embedding_preset_name", Text, nullable=False, server_default=text("''")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    UniqueConstraint("mailbox_id", "interval_start_date", "period_days"),
    CheckConstraint("period_days > 0"),
)

interval_summary_embeddings = Table(
    "interval_summary_embeddings",
    metadata,
    Column("summary_id", BigInteger, ForeignKey("mailbox_interval_summaries.id", ondelete="CASCADE"), nullable=False),
    Column("preset_name", Text, nullable=False),
    Column("embedding_model", Text, nullable=False),
    Column("input_hash", Text, nullable=False),
    Column("dimensions", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'ready'")),
    Column("embedding", Vector),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    PrimaryKeyConstraint("summary_id", "embedding_model"),
)

attachments = Table(
    "attachments",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
    Column("provider_attachment_id", Text),
    Column("filename", Text, nullable=False),
    Column("mime_type", Text, nullable=False),
    Column("size_bytes", Integer, nullable=False, server_default=text("0")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
)

jobs = Table(
    "jobs",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("job_type", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'running'")),
    Column("phase", Text, nullable=False, server_default=text("'starting'")),
    Column("total_count", Integer, nullable=False, server_default=text("0")),
    Column("processed_count", Integer, nullable=False, server_default=text("0")),
    Column("current_item", Text, nullable=False, server_default=text("''")),
    Column("error_text", Text, nullable=False, server_default=text("''")),
    Column("metadata_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", DateTime(timezone=True)),
)

smart_search_jobs = Table(
    "smart_search_jobs",
    metadata,
    Column("job_id", BigInteger, ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True),
    Column("question", Text, nullable=False),
    Column("idempotency_key", Text, unique=True),
    Column("status", Text, nullable=False, server_default=text("'queued'")),
    Column("failed_stage", Text),
    Column("error_code", Text),
    Column("safe_error_text", Text, nullable=False, server_default=text("''")),
    Column("retryable", Boolean, nullable=False, server_default=text("false")),
    Column("cancel_requested", Boolean, nullable=False, server_default=text("false")),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("attempt_count", Integer, nullable=False, server_default=text("0")),
    Column("planner_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("embedding_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("compressor_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("answer_preset_id", BigInteger, ForeignKey("llm_presets.id", ondelete="SET NULL")),
    Column("embedding_model", Text, nullable=False, server_default=text("''")),
    Column("owner_timezone", Text, nullable=False, server_default=text("'UTC'")),
    Column("embedding_dimensions", Integer),
    Column("preset_fingerprints_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("work_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("matching_sources_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("rejected_message_ids_json", JSONB, nullable=False, server_default=text("'[]'::jsonb")),
    Column("plan_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("stage_trace_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("coverage_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("result_json", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired')"),
)

smart_search_job_messages = Table(
    "smart_search_job_messages",
    metadata,
    Column("job_id", BigInteger, ForeignKey("smart_search_jobs.job_id", ondelete="CASCADE"), nullable=False),
    Column("message_id", BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
    Column("source_hash", Text, nullable=False),
    PrimaryKeyConstraint("job_id", "message_id"),
)

Index("idx_messages_mailbox", messages.c.mailbox_id)
Index("idx_messages_received", messages.c.received_at.desc())
Index("idx_interest_matches_profile", message_interest_matches.c.interest_profile_id, message_interest_matches.c.primary_match.desc(), message_interest_matches.c.score.desc())
Index("idx_topic_memberships_cluster", message_topic_memberships.c.topic_cluster_id, message_topic_memberships.c.is_primary.desc(), message_topic_memberships.c.score.desc())
Index("idx_embeddings_status", message_embeddings.c.status)
Index("idx_embeddings_model_dimensions_status", message_embeddings.c.embedding_model, message_embeddings.c.dimensions, message_embeddings.c.status)
Index("idx_topic_clusters_model_dimensions", topic_clusters.c.embedding_model, topic_clusters.c.dimensions)
Index("idx_topic_clusters_frontier", topic_clusters.c.embedding_model, topic_clusters.c.frontier_at.desc(), topic_clusters.c.id.desc())
Index("idx_message_clustering_work_claim", message_clustering_work.c.embedding_model, message_clustering_work.c.status, message_clustering_work.c.retry_at, message_clustering_work.c.message_id)
Index("idx_mailbox_interval_summaries_lookup", mailbox_interval_summaries.c.mailbox_id, mailbox_interval_summaries.c.period_days, mailbox_interval_summaries.c.interval_start_date.desc())
Index("idx_mailbox_interval_summaries_stale", mailbox_interval_summaries.c.period_days, mailbox_interval_summaries.c.refreshed_at, mailbox_interval_summaries.c.interval_start_date)
Index("idx_interval_summary_embeddings_status", interval_summary_embeddings.c.status)
Index("idx_interval_summary_embeddings_model_dimensions_status", interval_summary_embeddings.c.embedding_model, interval_summary_embeddings.c.dimensions, interval_summary_embeddings.c.status)
Index("idx_jobs_status", jobs.c.status, jobs.c.updated_at.desc())
Index("idx_jobs_type_updated", jobs.c.job_type, jobs.c.updated_at.desc(), jobs.c.id.desc())
Index("idx_smart_search_jobs_claim", smart_search_jobs.c.status, smart_search_jobs.c.lease_expires_at, smart_search_jobs.c.job_id)
Index("idx_smart_search_jobs_updated", smart_search_jobs.c.updated_at.desc(), smart_search_jobs.c.job_id.desc())
Index("idx_smart_search_job_messages_message", smart_search_job_messages.c.message_id, smart_search_job_messages.c.job_id)

PRE_TABLE_DDL = (
    "CREATE EXTENSION IF NOT EXISTS vector",
)

SPECIAL_DDL = (
    """
CREATE INDEX IF NOT EXISTS idx_messages_star ON messages (received_at DESC NULLS LAST, id DESC) WHERE star > 0
    """,
    """
CREATE INDEX IF NOT EXISTS idx_messages_search_document ON messages USING GIN(search_document)
    """,
    """
CREATE INDEX IF NOT EXISTS idx_embeddings_hnsw_1024_cosine ON message_embeddings USING hnsw ((embedding::vector(1024)) vector_cosine_ops) WHERE status = 'ready' AND dimensions = 1024
    """,
    """
CREATE INDEX IF NOT EXISTS idx_mailbox_interval_summaries_search_document ON mailbox_interval_summaries USING GIN(search_document)
    """,
    """
CREATE INDEX IF NOT EXISTS idx_interval_summary_embeddings_hnsw_1024_cosine ON interval_summary_embeddings USING hnsw ((embedding::vector(1024)) vector_cosine_ops) WHERE status = 'ready' AND dimensions = 1024
    """,
    """
CREATE INDEX IF NOT EXISTS idx_mailbox_interval_summaries_request_rebuild ON mailbox_interval_summaries(request_rebuild) WHERE request_rebuild IS NOT NULL
    """,
)

SEED_DDL = (
    """
    INSERT INTO pipeline_settings (id, analysis_mode, embedding_mode, canonical_embedding_model)
    VALUES (1, 'priority', 'priority', '')
    ON CONFLICT (id) DO NOTHING
    """,
    """
    INSERT INTO llm_presets (name, provider, enabled, priority)
    SELECT 'Local Ollama', 'ollama', false, 100
    WHERE NOT EXISTS (SELECT 1 FROM llm_presets)
    ON CONFLICT (name) DO NOTHING
    """,
)


QUEUE_DDL = (
    """
CREATE TABLE IF NOT EXISTS work_queue (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          kind text NOT NULL, dedup_key text NOT NULL UNIQUE,
          payload jsonb NOT NULL DEFAULT '{}'::jsonb,
          priority integer NOT NULL DEFAULT 2,
          state text NOT NULL DEFAULT 'new' CHECK(state IN ('new','processing','paused')),
          revision bigint NOT NULL DEFAULT 1, claimed_revision bigint,
          retries integer NOT NULL DEFAULT 0, worker_id text,
          preset_id bigint REFERENCES llm_presets(id) ON DELETE SET NULL,
          target_preset_id bigint REFERENCES llm_presets(id) ON DELETE CASCADE,
          source_id bigint REFERENCES mail_accounts(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now()
        )
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_processing ON work_queue(id)
      WHERE state='processing'
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_kind_new ON work_queue(kind, priority, id)
      WHERE state='new' AND target_preset_id IS NULL
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_target_new ON work_queue(target_preset_id, kind, priority, id)
      WHERE state='new' AND target_preset_id IS NOT NULL
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_model_new ON work_queue
      (kind, (COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','')), priority, id)
      WHERE state='new' AND target_preset_id IS NULL
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_target_model_new ON work_queue
      (target_preset_id, kind, (COALESCE(payload->>'embedding_model',payload->>'canonical_embedding_model','')), priority, id)
      WHERE state='new' AND target_preset_id IS NOT NULL
    """,
    """
CREATE TABLE IF NOT EXISTS queue_workers (
          worker_id text PRIMARY KEY, state text NOT NULL,
          preset_id bigint REFERENCES llm_presets(id) ON DELETE CASCADE,
          source_id bigint REFERENCES mail_accounts(id) ON DELETE CASCADE,
          reason text NOT NULL DEFAULT '', changed_at timestamptz NOT NULL DEFAULT now()
        )
    """,
    """
CREATE TABLE IF NOT EXISTS queue_events (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          occurred_at timestamptz NOT NULL DEFAULT now(), action text NOT NULL,
          job_id bigint, kind text, worker_id text, preset_id bigint, source_id bigint,
          retries integer, reason text NOT NULL DEFAULT ''
        )
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_priority_id ON work_queue(priority,id)
    """,
    """
CREATE INDEX IF NOT EXISTS work_queue_state_priority_id ON work_queue(state,priority,id)
    """,
)


def baseline_statements() -> list[str]:
    dialect = postgresql.dialect()
    statements: list[str] = list(PRE_TABLE_DDL)
    statements.extend(
        str(CreateTable(table, if_not_exists=True).compile(dialect=dialect))
        for table in metadata.sorted_tables
    )
    statements.extend(QUEUE_DDL)
    statements.extend(
        str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect))
        for table in metadata.sorted_tables
        for index in table.indexes
    )
    statements.extend(SPECIAL_DDL)
    statements.extend(item.strip() for item in SEED_DDL)
    return statements
