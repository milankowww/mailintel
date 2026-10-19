from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SyncCursor:
    mailbox_id: int
    cursor_value: str | None
    updated_at: datetime | None = None


@dataclass(slots=True)
class MailAccount:
    id: int | None
    name: str
    provider_type: str
    auth_mode: str
    secret_locator: str | None
    settings: dict[str, Any] = field(default_factory=dict)
    encrypted_password: str | None = None
    password_encryption: str | None = None
    status: str = "active"


@dataclass(slots=True)
class Mailbox:
    id: int | None
    account_id: int
    provider_mailbox_id: str
    name: str
    monitored: bool = True
    sync_cursor: str | None = None
    last_synced_at: datetime | None = None
    crawl_status: str = "idle"
    crawl_lease_expires_at: datetime | None = None
    crawl_attempt_count: int = 0
    crawl_last_error: str = ""
    next_sync_at: datetime | None = None
    last_crawled_at: datetime | None = None


@dataclass(slots=True)
class UserProfile:
    name: str
    role: str = ""
    organization: str = ""
    aliases: list[str] = field(default_factory=list)
    email_addresses: list[str] = field(default_factory=list)
    collaborators: list[str] = field(default_factory=list)
    communication_preferences: str = ""
    about: str = ""


CAPABILITY_MODEL_FIELDS = {
    "mail_summary": "text_model",
    "mail_embedding": "embedding_model",
    "daily_summary": "text_model",
    "rollup_summary": "text_model",
    "topic_label": "text_model",
    "interval_embedding": "embedding_model",
    "search_plan": "text_model",
    "search_embedding": "embedding_model",
    "search_compress": "text_model",
    "search_answer": "text_model",
}
ALL_PRESET_CAPABILITIES = tuple(CAPABILITY_MODEL_FIELDS)


@dataclass(slots=True)
class LLMPreset:
    id: int | None = None
    name: str = "Local Ollama"
    provider: str = "ollama"
    enabled: bool = False
    priority: int = 100
    auth_mode: str = "env"
    secret_locator: str = ""
    endpoint_url: str = "http://127.0.0.1:11434"
    text_model: str = "llama3.1"
    embedding_model: str = "nomic-embed-text"
    temperature: float = 0.2
    text_context_window: int = 8192
    embedding_context_window: int = 8192
    max_output_tokens: int = 800
    initial_timeout_seconds: float | None = None
    streaming_timeout_seconds: float | None = None
    notes: str = ""
    concurrency: int = 1
    capabilities: list[str] = field(default_factory=lambda: list(ALL_PRESET_CAPABILITIES))


@dataclass(slots=True)
class PipelineSettings:
    canonical_embedding_model: str = ""


@dataclass(slots=True)
class PresetStageHealth:
    preset_id: int
    stage: str
    status: str = "healthy"
    consecutive_failures: int = 0
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    cooldown_until: datetime | None = None
    last_error_text: str = ""


@dataclass(slots=True)
class ClusterRebuildState:
    embedding_model: str
    status: str = "idle"
    dirty_since: datetime | None = None
    lease_expires_at: datetime | None = None
    attempt_count: int = 0
    last_error_text: str = ""
    last_completed_at: datetime | None = None
    last_ready_embedding_count: int = 0


@dataclass(slots=True)
class InterestProfile:
    id: int | None
    name: str
    description: str
    collaborators: list[str] = field(default_factory=list)
    signals: str = ""
    priority_weight: float = 1.0
    dashboard_prominence: int = 1
    summary_detail: str = "standard"
    watch_mode: str = "watch"


@dataclass(slots=True)
class MessageRef:
    provider_message_id: str
    unread: bool
    received_at: datetime | None = None
    thread_id: str | None = None
    sync_token: str | None = None


@dataclass(slots=True)
class AttachmentDescriptor:
    provider_attachment_id: str | None
    filename: str
    mime_type: str
    size_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedMessage:
    provider_message_id: str
    mailbox_provider_id: str
    internet_message_id: str | None
    thread_id: str | None
    subject: str
    sender_name: str
    sender_address: str
    recipients: list[str]
    cc: list[str]
    received_at: datetime | None
    sent_at: datetime | None
    unread: bool
    has_attachments: bool
    importance_hint: str | None
    snippet_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MessageBundle:
    message: NormalizedMessage
    attachments: list[AttachmentDescriptor]
    body_text: str
    body_source_segment: str = "body"


@dataclass(slots=True)
class MessageSummary:
    message_id: int | None
    summary_text: str
    importance_score: float
    rationale: str
    confidence: float
    model_name: str


@dataclass(slots=True)
class InterestMatch:
    interest_profile_id: int
    score: float
    rationale: str
    primary_match: bool = False


@dataclass(slots=True)
class TopicCluster:
    id: int | None
    topic_key: str
    name: str
    description: str
    key_terms: list[str]
    importance_score: float
    key_entities: list[str] = field(default_factory=list)
    llm_summary: str = ""
    why_it_matters: str = ""
    model_name: str = ""
    embedding_model: str = ""


@dataclass(slots=True)
class TopicMembership:
    topic_cluster_id: int
    score: float
    is_primary: bool = False


@dataclass(slots=True)
class TriageScore:
    message_id: int
    importance_score: float
    rationale: str
    primary_interest_profile_id: int | None
    primary_topic_cluster_id: int | None


@dataclass(slots=True)
class ChatSource:
    type: str
    id: int
    title: str
    snippet: str
    date: str = ""
    mailbox: str = ""
    subject: str = ""
    sender_name: str = ""
    sender_address: str = ""
    received_at: str = ""
    summary_text: str = ""
    preview_text: str = ""
    message_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ChatResponse:
    answer: str
    sources: list[ChatSource]
    thought_text: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class JobRecord:
    id: int | None
    job_type: str
    status: str = "running"
    phase: str = "starting"
    total_count: int = 0
    processed_count: int = 0
    current_item: str = ""
    error_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
