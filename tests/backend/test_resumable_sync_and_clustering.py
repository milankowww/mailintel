# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from types import SimpleNamespace

from datetime import UTC, datetime, timedelta
import logging

import pytest

from mailintel.connectors.base import ConnectorAuthenticationError, ConnectorConnectionError
from mailintel.models import JobRecord, TopicCluster
from mailintel.models import LLMPreset, Mailbox, MessageBundle, MessageSummary, NormalizedMessage
from mailintel.services.llm import LLMContentValidationError, LLMInputError
from mailintel.services import pipeline as pipeline_module
from mailintel.services.pipeline import INTERVAL_SUMMARY_MERGE_FALLBACK_WARNING
from mailintel.services.llm import MessageAnalysis
from mailintel.services.pipeline import PipelineRuntime
from mailintel.services.topics import TopicClusteringService
from mailintel.settings import AppSettings


class IncrementalClusteringRepository:
    def __init__(self, *, best=None):
        self.best = best
        self.assigned = None
        self.published = None

    def get_incremental_clustering_message(self, message_id, embedding_model):
        assert embedding_model == "embed-model"
        return {
            "id": message_id,
            "subject": "Security incident follow-up",
            "summary_text": "Follow-up about account access",
            "cleaned_text": "Security incident account access",
            "importance_score": 0.8,
            "embedding_text": "[1,0]",
            "input_hash": "source-1",
            "received_at": datetime(2026, 8, 5, tzinfo=UTC),
        }

    def best_incremental_topic_cluster(self, message_id, embedding_model):
        assert message_id == 42
        assert embedding_model == "embed-model"
        return self.best

    def assign_incremental_message_to_cluster(self, **kwargs):
        self.assigned = kwargs

    def list_llm_presets(self, enabled_only=False):
        assert enabled_only is True
        return []

    def publish_incremental_topic_cluster(self, **kwargs):
        self.published = kwargs
        return 11


def test_incremental_clustering_assigns_to_recent_matching_cluster_without_relabeling():
    repository = IncrementalClusteringRepository(best={"id": 9, "score": 0.91})

    result = TopicClusteringService(repository).process_incremental_message(
        message_id=42,
        embedding_model="embed-model",
        source_hash="source-1",
    )

    assert result["action"] == "assigned"
    assert result["topic_cluster_id"] == 9
    assert repository.assigned == {
        "message_id": 42,
        "embedding_model": "embed-model",
        "algorithm_version": "recent-centroid-v1",
        "source_hash": "source-1",
        "topic_cluster_id": 9,
        "score": 0.91,
    }
    assert repository.published is None


def test_incremental_clustering_creates_and_publishes_cluster_when_frontier_does_not_match():
    repository = IncrementalClusteringRepository(best={"id": 9, "score": 0.4})

    with pytest.raises(RuntimeError, match="requires a model label"):
        TopicClusteringService(repository).process_incremental_message(
            message_id=42, embedding_model="embed-model", source_hash="source-1",
        )
    with pytest.raises(RuntimeError, match="requires a model label"):
        TopicClusteringService(repository).process_incremental_message(
            message_id=42, embedding_model="embed-model", source_hash="source-1",
            labelled=TopicCluster(id=None, topic_key="fallback", name="Fallback",
                                  description="Fallback", key_terms=["fallback"],
                                  importance_score=0.5),
        )
    assert repository.published is None

    result = TopicClusteringService(repository).process_incremental_message(
        message_id=42,
        embedding_model="embed-model",
        source_hash="source-1",
        labelled=TopicCluster(id=None, topic_key="model-key", name="Model topic",
                              description="Model description", key_terms=["model"],
                              importance_score=0.5, model_name="test:model"),
    )

    assert result == {
        "action": "created",
        "message_id": 42,
        "topic_cluster_id": 11,
        "score": 1.0,
    }
    assert repository.assigned is None
    assert repository.published["message_id"] == 42
    assert repository.published["source_hash"] == "source-1"
    assert repository.published["cluster"].topic_key


def test_incremental_clustering_rejects_stale_claim_before_writing_membership():
    repository = IncrementalClusteringRepository(best={"id": 9, "score": 0.91})

    try:
        TopicClusteringService(repository).process_incremental_message(
            message_id=42,
            embedding_model="embed-model",
            source_hash="older-source",
        )
    except RuntimeError as exc:
        assert "embedding changed" in str(exc)
    else:
        raise AssertionError("stale clustering claim should fail")

    assert repository.assigned is None
    assert repository.published is None


class ResumeRepository:
    def __init__(self):
        self.llm_rows = [
            {
                "id": 1,
                "name": "Local Ollama",
                "provider": "ollama",
                "enabled": True,
                "priority": 1,
                "auth_mode": "env",
                "secret_locator": "",
                "endpoint_url": "http://127.0.0.1:11434",
                "text_model": "chat-v1",
                "embedding_model": "embed-v1",
                "temperature": 0.2,
                "max_output_tokens": 800,
                "notes": "",
            }
        ]
        self.jobs = []
        self.messages = {}
        self.processing_state = {}
        self.summaries = {}
        self.embeddings = {}
        self.cursor_updates = []

    def list_interest_profiles(self):
        return []

    def list_llm_presets(self, enabled_only=False):
        return self.llm_rows if enabled_only else self.llm_rows

    def expire_stale_running_jobs(self, _updated_before):
        return 0

    def get_running_sync_job_for_account(self, _account_id):
        return None

    def list_mailboxes(self, account_id=None, monitored_only=False):
        return [
            {
                "id": 10,
                "account_id": int(account_id or 1),
                "provider_mailbox_id": "INBOX",
                "name": "Inbox",
                "monitored": True,
                "sync_cursor": None,
            }
        ]

    def list_messages_needing_llm_summary(self, _mailbox_id):
        return []

    def message_processing_complete(self, *, mailbox_id, provider_message_id, embedding_model):
        del mailbox_id, provider_message_id
        state = self.processing_state.get(1)
        if not state:
            return False
        if state["interest_status"] != "done" or state["summary_status"] != "done":
            return False
        if not embedding_model:
            return True
        return state["embedding_status"] == "done" and state["embedding_model"] == embedding_model

    def create_job(self, job):
        self.jobs.append(job)
        return len(self.jobs)

    def update_job(self, _job_id, **_kwargs):
        pass

    def upsert_message(self, mailbox_id, message, body_text, attachments=None):
        del attachments
        message_id = 1
        self.messages[message_id] = {
            "id": message_id,
            "mailbox_id": mailbox_id,
            "subject": message.subject,
            "sender_name": message.sender_name,
            "sender_address": message.sender_address,
            "summary_text": self.summaries.get(message_id, ""),
            "cleaned_text": body_text,
        }
        self.processing_state.setdefault(
            message_id,
            {
                "message_id": message_id,
                "interest_status": "pending",
                "interest_input_hash": "",
                "summary_status": "pending",
                "summary_input_hash": "",
                "embedding_status": "pending",
                "embedding_input_hash": "",
                "embedding_model": "",
            },
        )
        return message_id

    def get_message_processing_state(self, message_id):
        return dict(self.processing_state[message_id])

    def replace_interest_matches(self, _message_id, _matches):
        pass

    def mark_interest_matches_done(self, message_id, input_text):
        self.processing_state[message_id]["interest_status"] = "done"
        self.processing_state[message_id]["interest_input_hash"] = input_text

    def mark_summary_pending(self, message_id):
        self.processing_state[message_id]["summary_status"] = "pending"
        self.processing_state[message_id]["summary_input_hash"] = ""
        if self.processing_state[message_id]["embedding_status"] == "done":
            self.processing_state[message_id]["embedding_status"] = "pending"

    def upsert_summary(self, summary):
        self.summaries[summary.message_id] = summary.summary_text
        self.messages[summary.message_id]["summary_text"] = summary.summary_text

    def mark_summary_done(self, message_id, input_text):
        self.processing_state[message_id]["summary_status"] = "done"
        self.processing_state[message_id]["summary_input_hash"] = input_text

    def get_message_detail(self, message_id):
        return dict(self.messages[message_id])

    def embedding_input_for_row(self, row):
        return f"{row['subject']}|{row['summary_text']}|{row['cleaned_text']}"

    def upsert_message_embedding(self, *, message_id, preset_name, embedding_model, input_text, vector):
        self.embeddings[(message_id, embedding_model)] = {
            "preset_name": preset_name,
            "input_text": input_text,
            "vector": vector,
        }

    def update_mailbox_cursor(self, mailbox_id, cursor_value):
        self.cursor_updates.append((mailbox_id, cursor_value))


def test_mark_summary_pending_invalidates_completed_embedding():
    repository = ResumeRepository()
    mailbox = Mailbox(id=1, account_id=1, provider_mailbox_id="INBOX", name="Inbox", monitored=True, sync_cursor=None)
    message = NormalizedMessage(
        provider_message_id="msg-1",
        mailbox_provider_id="INBOX",
        internet_message_id=None,
        thread_id=None,
        subject="Subject",
        sender_name="Sender",
        sender_address="sender@example.com",
        recipients=[],
        cc=[],
        received_at=None,
        sent_at=None,
        unread=True,
        has_attachments=False,
        importance_hint=None,
        snippet_hash="abc",
        metadata={},
    )
    message_id = repository.upsert_message(mailbox.id, message, body_text="Excerpt")
    repository.mark_summary_done(message_id, "summary-input")
    repository.processing_state[message_id]["embedding_status"] = "done"
    repository.processing_state[message_id]["embedding_input_hash"] = "embedding-input"
    repository.processing_state[message_id]["embedding_model"] = "embed"

    repository.mark_summary_pending(message_id)

    state = repository.get_message_processing_state(message_id)
    assert state["summary_status"] == "pending"
    assert state["embedding_status"] == "pending"












class AnalysisFallbackRepository:
    def __init__(self):
        self.summary = None
        self.completed = None
        self.replaced_matches = None

    def get_analysis_message_detail(self, message_id):
        return {
            "id": message_id,
            "provider_message_id": f"msg-{message_id}",
            "provider_mailbox_id": "INBOX",
            "subject": "",
            "sender_name": "",
            "sender_address": "",
            "recipients_json": [],
            "cc_json": [],
            "preview_text": "",
            "body_text": "",
            "is_read": False,
            "has_attachments": False,
            "importance_hint": None,
            "snippet_hash": "hash",
        }

    def replace_interest_matches(self, message_id, matches):
        self.replaced_matches = (message_id, matches)

    def upsert_summary(self, summary):
        self.summary = summary

    def complete_analysis(self, message_id, **kwargs):
        self.completed = (message_id, kwargs)


class MailboxRetryRepository:
    def rollback(self):
        pass

    def queue_set_worker(self, *_args, **_kwargs):
        pass

    def _execute(self, *_args, **_kwargs):
        return SimpleNamespace(fetchone=lambda: {"exists": True})

    def __init__(self, provider_type="imap"):
        self.provider_type = provider_type
        self.released = None
        self.updated_jobs = []

    def get_mail_account(self, _account_id):
        return {"id": 1, "provider_type": self.provider_type, "settings_json": {}}

    def create_job(self, _job):
        return 1

    def message_processing_complete(self, **_kwargs):
        return True

    def message_missing_received_at(self, **_kwargs):
        return False

    def update_mailbox_cursor(self, *_args):
        pass

    def release_mailbox_claim(self, mailbox_id, **kwargs):
        self.released = (mailbox_id, kwargs)

    def update_job(self, job_id, **kwargs):
        self.updated_jobs.append((job_id, kwargs))


def _mailbox_row(crawl_attempt_count=1):
    return {
        "id": 10,
        "account_id": 1,
        "provider_mailbox_id": "INBOX",
        "name": "Inbox",
        "monitored": True,
        "sync_cursor": None,
        "last_synced_at": None,
        "crawl_attempt_count": crawl_attempt_count,
    }


def test_mailbox_no_new_mail_retries_in_30_seconds():
    repository = MailboxRetryRepository()
    runtime = PipelineRuntime(AppSettings(sync_interval_seconds=120), password_encryptor=None)

    class NoNewMailConnector:
        def list_message_refs(self, *_args, **_kwargs):
            return []

        def get_sync_cursor(self, mailbox):
            return mailbox.sync_cursor

    runtime._create_connector = lambda _account: NoNewMailConnector()
    runtime._process_mailbox(repository, _mailbox_row(), canonical_model="embed-model")

    assert repository.released is not None
    _mailbox_id, release = repository.released
    assert release["status"] == "completed"
    assert release["next_sync_at"] is not None
    delay = release["next_sync_at"] - datetime.now(UTC)
    assert timedelta(seconds=25) <= delay <= timedelta(seconds=30)


def test_authentication_failure_stops_mailbox_auto_retry():
    repository = MailboxRetryRepository()
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None)

    class AuthFailedConnector:
        def list_message_refs(self, *_args, **_kwargs):
            raise ConnectorAuthenticationError("IMAP login failed for user. Check the password.")

    runtime._create_connector = lambda _account: AuthFailedConnector()
    runtime._process_mailbox(repository, _mailbox_row(), canonical_model="embed-model")

    assert repository.released is not None
    _mailbox_id, release = repository.released
    assert release["status"] == "failed"
    assert release["next_sync_at"] is None
    assert release["clear_next_sync_at"] is True
    assert "Automatic sync stopped" in release["error_text"]


def test_unreachable_mailbox_backoff_sequence():
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None)
    error = ConnectorConnectionError("unreachable")

    assert [runtime._mailbox_failure_retry_seconds(error, attempt) for attempt in range(1, 7)] == [1, 1, 10, 20, 60, 60]


def test_unreachable_mailbox_primary_error_is_friendly_and_job_keeps_detail():
    repository = MailboxRetryRepository(provider_type="ews")
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None)

    class UnreachableConnector:
        def list_message_refs(self, *_args, **_kwargs):
            raise ConnectorConnectionError(
                "EWS endpoint is unreachable: <urlopen error [SSL: UNEXPECTED_EOF_WHILE_READING] low-level detail>",
                operator_message="EWS endpoint is unreachable.",
            )

    runtime._create_connector = lambda _account: UnreachableConnector()
    runtime._process_mailbox(repository, _mailbox_row(), canonical_model="embed-model")

    assert repository.released is not None
    _mailbox_id, release = repository.released
    assert release["status"] == "failed"
    assert release["next_sync_at"] is not None
    assert release["error_text"] == "EWS endpoint is unreachable. Automatic sync will retry in about 1 second; you can retry manually sooner."
    assert "UNEXPECTED_EOF_WHILE_READING" not in release["error_text"]
    failed_job_updates = [kwargs for _job_id, kwargs in repository.updated_jobs if kwargs.get("status") == "failed"]
    assert failed_job_updates
    assert "UNEXPECTED_EOF_WHILE_READING" in failed_job_updates[-1]["error_text"]






def test_interval_summary_source_batches_bound_large_days():
    line_count = pipeline_module.INTERVAL_SUMMARY_INPUT_BATCH_SIZE + 1
    source_text = "\n".join(f"- mail_id={index}; summary {index}" for index in range(line_count))

    batches = PipelineRuntime._interval_summary_source_batches(source_text)

    assert len(batches) == 2
    assert batches[0].count("\n") + 1 == pipeline_module.INTERVAL_SUMMARY_INPUT_BATCH_SIZE
    assert batches[1] == f"- mail_id={line_count - 1}; summary {line_count - 1}"










def test_analysis_input_error_uses_fallback_without_cooling_down_engine(monkeypatch):
    repository = AnalysisFallbackRepository()
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("engine health should not be marked unavailable for bad message input")

    row = {
        "id": 1,
        "name": "Local Ollama",
        "provider": "ollama",
        "enabled": True,
        "priority": 1,
        "auth_mode": "env",
        "secret_locator": "",
        "endpoint_url": "http://127.0.0.1:11434",
        "text_model": "qwen3:4b",
        "embedding_model": "qwen3-embedding:0.6b",
        "temperature": 0.2,
        "text_context_window": 8192,
        "max_output_tokens": 800,
        "notes": "",
        "health_status": "healthy",
        "consecutive_failures": 0,
    }

    runtime.llm_service = SimpleNamespace(analyze_message=lambda *_args, **_kwargs: (_ for _ in ()).throw(LLMInputError("bad input")))
    repository.list_interest_profiles = lambda: []
    repository.get_pipeline_settings = lambda: {}
    repository.rollback = lambda: None
    repository.get_message_processing_state = lambda _id: {"source_input_hash": "source-100"}
    repository._execute = lambda *_args: SimpleNamespace(fetchone=lambda: repository.get_message_processing_state(100))
    runtime._handle_analysis(repository, {"payload": {"message_id": 100, "source_input_hash": "source-100"}, "priority": 1}, LLMPreset(id=1, name="Local", enabled=True))()

    assert repository.replaced_matches == (100, [])
    assert repository.summary is not None
    assert repository.summary.message_id == 100
    assert repository.summary.model_name == "rules/v1"
    assert repository.completed is not None
    assert repository.completed[0] == 100
    assert repository.completed[1]["preset_name"] == "Local"


def test_summary_user_context_excludes_communication_preferences():
    class Repository:
        def get_user_profile(self):
            return {
                "name": "Milan",
                "aliases_json": ["milanp"],
                "email_addresses_json": ["milan@example.test"],
                "role": "Analyst",
                "organization": "Example Org",
                "about": "Works on mailbox intelligence.",
                "communication_preferences": "Put deadlines first.",
            }

    runtime = PipelineRuntime(AppSettings(), password_encryptor=None)

    context = runtime._summary_user_context(Repository())

    assert "Mailbox owner name: Milan" in context
    assert "Owner aliases/usernames: milanp" in context
    assert "Owner email addresses: milan@example.test" in context
    assert "Put deadlines first" not in context


def test_pipeline_passes_user_context_to_message_analysis(monkeypatch):
    class LLM:
        def __init__(self):
            self.user_context = ""

        def analyze_message(self, _preset, **kwargs):
            self.user_context = kwargs["user_context"]
            return MessageAnalysis(
                summary=MessageSummary(
                    message_id=None,
                    summary_text="Milan received the report.",
                    importance_score=0.5,
                    rationale="message says so",
                    confidence=0.8,
                    model_name="ollama:summary-v1",
                ),
                interest_matches=[],
            )

    class Repository:
        def get_user_profile(self):
            return {"name": "Milan", "aliases_json": [], "email_addresses_json": []}

        def get_analysis_message_detail(self, message_id):
            return {
                "id": message_id,
                "provider_message_id": f"msg-{message_id}",
                "provider_mailbox_id": "INBOX",
                "subject": "Report",
                "sender_name": "Sender",
                "sender_address": "sender@example.test",
                "recipients_json": [],
                "cc_json": [],
                "body_text": "Milan received the report.",
                "is_read": False,
                "has_attachments": False,
                "importance_hint": None,
                "snippet_hash": "hash",
            }

        def replace_interest_matches(self, _message_id, _matches):
            pass

        def upsert_summary(self, summary):
            self.summary = summary

        def complete_analysis(self, _message_id, **kwargs):
            self.completed = kwargs

    row = {
        "id": 1,
        "name": "Local",
        "provider": "ollama",
        "enabled": True,
        "priority": 1,
        "auth_mode": "env",
        "secret_locator": "",
        "endpoint_url": "http://127.0.0.1:11434",
        "text_model": "chat-v1",
        "embedding_model": "embed-v1",
        "temperature": 0.2,
        "text_context_window": 8192,
        "max_output_tokens": 800,
        "notes": "",
    }
    llm = LLM()
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None, llm_service=llm)
    repository = Repository()

    repository.list_interest_profiles = lambda: []
    repository.get_pipeline_settings = lambda: {}
    repository.rollback = lambda: None
    repository.get_message_processing_state = lambda _id: {"source_input_hash": "source-101"}
    repository._execute = lambda *_args: SimpleNamespace(fetchone=lambda: repository.get_message_processing_state(101))
    runtime._handle_analysis(repository, {"payload": {"message_id": 101, "source_input_hash": "source-101"}, "priority": 1}, LLMPreset(id=1, name="Local", enabled=True))()

    assert "Mailbox owner name: Milan" in llm.user_context
    interest_hash = pipeline_module._hash_step_input("Report", "Sender", "sender@example.test", "Milan received the report.")
    assert repository.completed["summary_input_hash"] == pipeline_module._hash_step_input(
        interest_hash,
        "Mailbox owner name: Milan",
        "Milan received the report.",
        "message says so",
    )


def test_pipeline_passes_user_context_to_interval_summary(monkeypatch):
    class LLM:
        def __init__(self):
            self.user_contexts = []

        def summarize_interval(self, _preset, **kwargs):
            self.user_contexts.append(kwargs["user_context"])
            return {"entries": [{"summary": "Milan-related report activity.", "message_ids": [101]}]}

    class Repository:
        def get_user_profile(self):
            return {"name": "Milan", "aliases_json": ["milanp"], "email_addresses_json": []}

        def mark_interval_summary_fresh(self, summary_id, summary_json):
            self.fresh = (summary_id, summary_json)

    row = {
        "id": 1,
        "name": "Local",
        "provider": "ollama",
        "enabled": True,
        "priority": 1,
        "auth_mode": "env",
        "secret_locator": "",
        "endpoint_url": "http://127.0.0.1:11434",
        "text_model": "chat-v1",
        "embedding_model": "embed-v1",
        "temperature": 0.2,
        "text_context_window": 8192,
        "max_output_tokens": 800,
        "notes": "",
    }
    llm = LLM()
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None, llm_service=llm)

    repository = Repository()
    run_interval_continuations(runtime,
        repository,
        {"id": 5, "summary_type": "daily", "interval_start_date": "2026-05-06", "period_days": 1, "mailbox_name": "Inbox"},
        {},
        "- mail_id=101; 2026-05-06; Sender; Report: Milan received the report.",
    )

    assert llm.user_contexts == ["Mailbox owner name: Milan\nOwner aliases/usernames: milanp"]
    assert repository.fresh[0] == 5




def test_interval_summary_stores_batch_entries_when_merge_pass_fails(monkeypatch):
    class LLM:
        def __init__(self):
            self.calls = 0

        def summarize_interval(self, _preset, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"entries": [{"summary": "First batch topic.", "message_ids": [101]}]}
            if self.calls == 2:
                return {"entries": [{"summary": "Second batch topic.", "message_ids": [102]}]}
            raise LLMContentValidationError("merge response was unusable")

    class Repository:
        def get_user_profile(self):
            return {}

        def mark_interval_summary_fresh(self, summary_id, summary_json):
            self.fresh = (summary_id, summary_json)

        def mark_interval_summary_merge_fallback(self, summary_id, summary_json):
            self.fallback = (summary_id, summary_json)

    row = {
        "id": 1,
        "name": "Local",
        "provider": "ollama",
        "enabled": True,
        "priority": 1,
        "auth_mode": "env",
        "secret_locator": "",
        "endpoint_url": "http://127.0.0.1:11434",
        "text_model": "chat-v1",
        "embedding_model": "embed-v1",
        "temperature": 0.2,
        "text_context_window": 8192,
        "max_output_tokens": 800,
        "notes": "",
    }
    llm = LLM()
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None, llm_service=llm)
    monkeypatch.setattr(pipeline_module, "INTERVAL_SUMMARY_INPUT_BATCH_SIZE", 1)

    repository = Repository()
    run_interval_continuations(runtime,
        repository,
        {
            "id": 5,
            "summary_type": "monthly",
            "interval_start_date": "2026-02-01",
            "period_days": 28,
            "mailbox_name": "Inbox",
        },
        {},
        "- mail_id=101; first source\n- mail_id=102; second source",
    )

    assert llm.calls == 3
    assert not hasattr(repository, "fresh")
    assert repository.fallback == (
        5,
        {
            "entries": [
                {"summary": "First batch topic.", "message_ids": [101]},
                {"summary": "Second batch topic.", "message_ids": [102]},
                {"summary": INTERVAL_SUMMARY_MERGE_FALLBACK_WARNING, "message_ids": []},
            ]
        },
    )


def test_interval_summary_merge_expands_synthetic_ids(monkeypatch):
    class LLM:
        def __init__(self):
            self.source_texts = []
            self.calls = 0

        def summarize_interval(self, _preset, **kwargs):
            self.calls += 1
            self.source_texts.append(kwargs["source_text"])
            if self.calls == 1:
                return {"entries": [{"summary": "Google account alerts.", "message_ids": [101, 102]}]}
            if self.calls == 2:
                return {"entries": [{"summary": "Microsoft account alerts.", "message_ids": [201]}]}
            return {"entries": [{"summary": "Account security alerts.", "message_ids": [1, 2]}]}

    class Repository:
        def get_user_profile(self):
            return {}

        def mark_interval_summary_fresh(self, summary_id, summary_json):
            self.fresh = (summary_id, summary_json)

        def mark_interval_summary_merge_fallback(self, summary_id, summary_json):
            self.fallback = (summary_id, summary_json)

    row = {
        "id": 1,
        "name": "Local",
        "provider": "ollama",
        "enabled": True,
        "priority": 1,
        "auth_mode": "env",
        "secret_locator": "",
        "endpoint_url": "http://127.0.0.1:11434",
        "text_model": "chat-v1",
        "embedding_model": "embed-v1",
        "temperature": 0.2,
        "text_context_window": 8192,
        "max_output_tokens": 800,
        "notes": "",
    }
    llm = LLM()
    runtime = PipelineRuntime(AppSettings(), password_encryptor=None, llm_service=llm)
    monkeypatch.setattr(pipeline_module, "INTERVAL_SUMMARY_INPUT_BATCH_SIZE", 1)

    repository = Repository()
    run_interval_continuations(runtime,
        repository,
        {
            "id": 5,
            "summary_type": "monthly",
            "interval_start_date": "2026-02-01",
            "period_days": 28,
            "mailbox_name": "Inbox",
        },
        {},
        "- mail_id=101; google source\n- mail_id=201; microsoft source",
    )

    assert "mail_id=1; Google account alerts." in llm.source_texts[2]
    assert "mail_id=2; Microsoft account alerts." in llm.source_texts[2]
    assert "101" not in llm.source_texts[2]
    assert repository.fresh == (
        5,
        {"entries": [{"summary": "Account security alerts.", "message_ids": [101, 102, 201]}]},
    )


def run_interval_continuations(runtime, repository, row, _settings, source):
    import re
    from mailintel.services.interval_queue import IntervalQueue

    repository.rollback = lambda: None
    repository.get_pipeline_settings = lambda: {}
    row = {"mailbox_id": 10, **row}
    # Build the same durable inputs both at enqueue and at publication.
    inputs = []
    for line in source.splitlines():
        message_id = int(re.search(r"mail_id=(\d+)", line).group(1))
        inputs.append({"id": message_id, "sender_name": "Sender", "subject": "Report",
                       "summary_text": line.split(";", 1)[1].strip()})
    repository.list_daily_summary_inputs = lambda *_args: inputs
    repository.list_rollup_summary_inputs = lambda *_args: [
        {"interval_start_date": row["interval_start_date"], "summary_json": {"entries": [
            {"summary": item["summary_text"], "message_ids": [item["id"]]}
        ]}} for item in inputs
    ]
    repository.get_interval_summary_embedding_detail = lambda _id: row
    repository._execute = lambda *_args: SimpleNamespace(fetchone=lambda: None)
    jobs = []
    repository.queue_enqueue = lambda kind, key, payload, **kwargs: jobs.append(
        {"kind": kind, "dedup_key": key, "payload": payload, "priority": kwargs["priority"]})
    queue = IntervalQueue(runtime)
    queue.enqueue(repository, row, runtime.settings.summary_timezone or "UTC")
    while jobs:
        job = jobs.pop(0)
        queue.handle(repository, job, LLMPreset(id=1, name="Local", enabled=True))()
