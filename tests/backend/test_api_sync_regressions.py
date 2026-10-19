from __future__ import annotations

from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mailintel.app import factory as api
from mailintel.models import Mailbox
from mailintel.settings import AppSettings


class FakeRepository:
    def __init__(self, _connection):
        self.connection = _connection
        FakeRepository.instance = self
        self.startup_calls = []
        self.refresh_calls = 0
        self.queue_activity_rows = {"jobs": [], "pending": []}
        self.triage_rows = []
        self.interest_profiles = []
        self.accounts = {
            1: {
                "id": 1,
                "name": "Active",
                "provider_type": "imap",
                "auth_mode": "config",
                "secret_locator": None,
                "encrypted_password": None,
                "password_encryption": None,
                "settings_json": {},
                "status": "active",
            },
            2: {
                "id": 2,
                "name": "Disabled",
                "provider_type": "imap",
                "auth_mode": "config",
                "secret_locator": None,
                "encrypted_password": None,
                "password_encryption": None,
                "settings_json": {},
                "status": "disabled",
            },
        }
        self.mailboxes = [
            {
                "id": 10,
                "account_id": 1,
                "provider_mailbox_id": "INBOX",
                "name": "Inbox",
                "monitored": True,
                "sync_cursor": None,
            }
        ]
        self.llm_presets = []
        self.next_llm_preset_id = 1
        self.pipeline_settings = {
            "analysis_mode": "priority",
            "embedding_mode": "priority",
            "canonical_embedding_model": "embed",
            "search_planner_preset_id": None,
            "search_embedding_preset_id": None,
            "search_compressor_preset_id": None,
            "search_answer_preset_id": None,
        }
        self.requested_interval_rebuilds = []
        self.pipeline_mailbox_statuses = [
            {
                "mailbox_id": 10,
                "account_id": 1,
                "account_name": "Active",
                "mailbox_name": "Inbox",
                "provider_mailbox_id": "INBOX",
                "monitored": True,
                "account_status": "active",
                "last_synced_at": None,
                "sync_status": "running",
                "crawl_status": "running",
                "crawl_job_status": "running",
                "crawl_last_error": "",
                "crawl_total_count": 24,
                "crawl_processed_count": 9,
                "total_messages": 110,
                "analyzed_done": 43,
                "embedded_done": 31,
            }
        ]
        self.topic_categories = {
            1: {
                "id": 1,
                "canonical_key": "embed:incident",
                "name": "Security Incident",
                "description": "Messages about a security incident.",
                "curated_name": "",
                "curated_description": "",
                "status": "active",
                "embedding_model": "embed",
                "key_terms_json": ["security", "incident"],
                "key_entities_json": [],
                "llm_summary": "",
                "why_it_matters": "",
                "importance_score": 0.9,
                "model_name": "",
                "cluster_count": 1,
                "message_count": 1,
                "matched_messages": [
                    {
                        "id": 501,
                        "subject": "Current security incident",
                        "received_at": "2026-05-10T12:00:00+00:00",
                        "topic_match_score": 0.9,
                    }
                ],
            }
        }

    def commit(self):
        pass

    def transaction(self):
        return nullcontext(self)

    def rollback(self):
        pass

    def mark_interrupted_jobs(self, _started_before):
        self.startup_calls.append("mark_interrupted_jobs")
        return 0

    def discard_unfinished_smart_searches(self):
        self.startup_calls.append("discard_unfinished_smart_searches")
        return 0

    def expire_stale_running_jobs(self, _updated_before):
        return 0

    def remove_rule_based_summaries(self):
        return 0

    def list_mail_accounts(self):
        return list(self.accounts.values())

    def get_mail_account(self, account_id):
        return self.accounts.get(account_id)

    def list_mailboxes(self, account_id=None, monitored_only=False):
        rows = self.mailboxes
        if account_id is not None:
            rows = [row for row in rows if row["account_id"] == account_id]
        if monitored_only:
            rows = [row for row in rows if row["monitored"]]
        return rows

    def upsert_mailbox(self, mailbox):
        self.mailboxes.append(
            {
                "id": len(self.mailboxes) + 10,
                "account_id": mailbox.account_id,
                "provider_mailbox_id": mailbox.provider_mailbox_id,
                "name": mailbox.name,
                "monitored": mailbox.monitored,
                "sync_cursor": mailbox.sync_cursor,
            }
        )
        return self.mailboxes[-1]["id"]

    def reset_processed_messages_for_accounts(self, _account_ids):
        return {"deleted_messages": 0}

    def delete_interval_summaries(self):
        return 7

    def request_interval_summary_rebuild(self, interval_start_date, period_days, summary_type):
        self.requested_interval_rebuilds.append((interval_start_date, period_days, summary_type))
        return [{"id": 701}, {"id": 702}]

    def list_llm_presets(self, enabled_only=False):
        presets = list(self.llm_presets)
        if enabled_only:
            presets = [preset for preset in presets if preset["enabled"]]
        return presets

    def upsert_llm_preset(self, preset):
        preset_id = preset.id or self.next_llm_preset_id
        if preset.id is None:
            self.next_llm_preset_id += 1
        record = {
            "id": preset_id,
            "name": preset.name,
            "provider": preset.provider,
            "enabled": preset.enabled,
            "priority": preset.priority,
            "auth_mode": preset.auth_mode,
            "secret_locator": preset.secret_locator,
            "endpoint_url": preset.endpoint_url,
            "text_model": preset.text_model,
            "embedding_model": preset.embedding_model,
            "temperature": preset.temperature,
            "text_context_window": preset.text_context_window,
            "embedding_context_window": preset.embedding_context_window,
            "max_output_tokens": preset.max_output_tokens,
            "notes": preset.notes,
            "concurrency": preset.concurrency,
            "capabilities_json": preset.capabilities,
        }
        self.llm_presets = [item for item in self.llm_presets if item["id"] != preset_id]
        self.llm_presets.append(record)
        self.llm_presets.sort(key=lambda item: (item["priority"], item["id"]))
        return preset_id, 0

    def get_account_sync_status(self, _account_id):
        return "idle"

    @staticmethod
    def _date_value(value):
        if value is None:
            return None
        text = str(value)
        if "T" in text:
            text = text.split("T", 1)[0]
        return date.fromisoformat(text)

    @classmethod
    def _within_date_range(cls, row, from_date=None, to_date=None):
        if from_date is None and to_date is None:
            return True
        received = cls._date_value(row.get("received_at"))
        if received is None:
            return False
        if from_date is not None and received < from_date:
            return False
        if to_date is not None and received > to_date:
            return False
        return True

    def list_recent_triage(self, limit=50, *, from_date=None, to_date=None):
        return [
            row
            for row in self.triage_rows
            if self._within_date_range(row, from_date=from_date, to_date=to_date)
        ][:limit]

    def list_interest_profiles(self):
        return list(self.interest_profiles)

    def get_active_llm_preset(self):
        enabled = [preset for preset in self.llm_presets if preset["enabled"]]
        if not enabled:
            return None
        enabled.sort(key=lambda item: (item["priority"], item["id"]))
        return enabled[0]

    def get_llm_preset(self, preset_id):
        return next(
            (preset for preset in self.llm_presets if int(preset["id"]) == int(preset_id)),
            None,
        )

    def get_clustering_status_counts(self, embedding_model):
        assert embedding_model == "embed"
        return {"ready_embeddings": 31, "clustered_messages": 18}

    def get_active_interval_summary_job(self):
        return None

    def get_pipeline_settings(self):
        return dict(self.pipeline_settings)

    def queue_activity(self):
        return self.queue_activity_rows

    def queue_snapshot(self):
        return {"jobs": [], "workers": [], "events": []}

    def queue_events(self, **_kwargs):
        return []

    def update_pipeline_settings(self, settings):
        self.pipeline_settings = {"canonical_embedding_model": settings.canonical_embedding_model}
        return dict(self.pipeline_settings)

    def list_stage_preset_health(self, stage, canonical_embedding_model=""):
        rows = []
        for preset in self.list_llm_presets(enabled_only=True):
            if stage == "embedding" and canonical_embedding_model and preset["embedding_model"] != canonical_embedding_model:
                continue
            rows.append(
                {
                    **preset,
                    "health_status": "healthy",
                    "consecutive_failures": 0,
                    "last_failure_at": None,
                    "last_success_at": None,
                    "cooldown_until": None,
                    "last_error_text": "",
                }
            )
        return rows

    def get_pipeline_status_counts(self, embedding_model):
        assert embedding_model == "embed"
        return {
            "analyses": {"total": 110, "done": 43, "running": 2, "queued": 65, "retrying": 0},
            "embeddings": {
                "total": 116,
                "done": 35,
                "running": 1,
                "queued": 5,
                "retrying": 0,
                "claimable": 2,
                "waiting_for_analysis": 75,
            },
            "message_embeddings": {
                "total": 110,
                "done": 31,
                "running": 1,
                "queued": 3,
                "retrying": 0,
                "claimable": 2,
                "waiting_for_analysis": 75,
            },
            "interval_summary_embeddings": {
                "total": 6,
                "done": 4,
                "running": 0,
                "queued": 2,
                "retrying": 0,
                "claimable": 0,
                "waiting_for_analysis": 0,
            },
            "summaries": {"total": 7, "done": 2, "running": 1, "queued": 4, "retrying": 0},
        }

    def get_cluster_pipeline_status(self, embedding_model):
        assert embedding_model == "embed"
        return {"ready_embeddings": 31, "clustered_messages": 18, "running": 1, "queued": 12, "retrying": 0, "status": "running"}

    def list_pipeline_mailbox_statuses(self, embedding_model):
        assert embedding_model == "embed"
        return list(self.pipeline_mailbox_statuses)

    def schedule_account_mailboxes(self, account_id):
        assert account_id == 1
        return 1

    def list_messages_for_interest_profile(self, profile_id, limit=10, *, from_date=None, to_date=None):
        profile = next((row for row in self.interest_profiles if int(row["id"]) == int(profile_id)), None)
        if profile is None:
            return []
        return [
            row
            for row in profile.get("matched_messages", [])
            if self._within_date_range(row, from_date=from_date, to_date=to_date)
        ][:limit]

    def list_dashboard_topics(self, *, limit=200, offset=0, from_date=None, to_date=None):
        rows = []
        for row in self.topic_categories.values():
            matched = [message for message in row.get("matched_messages", [])
                       if self._within_date_range(message, from_date=from_date, to_date=to_date)]
            if matched:
                rows.append({"id": row["id"], "name": row["name"], "message_count": len(matched)})
        return rows[offset:offset + limit]

    def get_topic_detail(self, topic_id):
        row = self.topic_categories.get(topic_id)
        if row is None:
            return None
        return {key: row.get(key) for key in
                ("id", "name", "description", "key_terms_json", "importance_score", "embedding_model")}

    def list_messages_for_topic(self, topic_id, limit=50, *, offset=0, from_date=None, to_date=None):
        rows = [row for row in self.topic_categories.get(topic_id, {}).get("matched_messages", [])
                if self._within_date_range(row, from_date=from_date, to_date=to_date)]
        return rows[offset:offset + limit]

    def rename_topic(self, topic_id, name):
        row = self.topic_categories.get(topic_id)
        if row is None:
            return None
        row["name"] = name
        return self.get_topic_detail(topic_id)

    def queue_enqueue(self, kind, dedup_key, payload, priority=5):
        self.enqueued_topic_job = (kind, dedup_key, payload, priority)
        return {"id": 55}


class FakeConnector:
    def list_mailboxes(self):
        return [Mailbox(id=None, account_id=0, provider_mailbox_id="Archive", name="Archive")]


class FakeConnectorRegistry:
    def create(self, _provider_type, _settings, _secret_locator):
        return FakeConnector()


class FakeChatService:
    def __init__(self, _repository, _settings, **_kwargs):
        pass

    def answer_query(self, _query):
        return SimpleNamespace(answer="ok", thought_text="", sources=[])


class FakePipelineRuntime:
    def register_queue_handlers(self, runtime):
        runtime.register("analyze_message", lambda *_args: None, capability="mail_summary")
        runtime.register("embed_message", lambda *_args: None, operation="embedding", model="embedding", capability="mail_embedding")

    def __init__(self, _settings, _password_encryptor, _connector_registry, **_kwargs):
        pass

    def start(self):
        return None

    def stop(self):
        return None

    def schedule_account_sync(self, account_id):
        return {"status": "scheduled", "scheduled_mailboxes": 1, "job_id": int(account_id) + 100}

    def request_embedding_refresh(self):
        return {"status": "scheduled", "marked_messages": 12, "marked_interval_summaries": 3, "job_id": 1}

    def request_analysis_refresh(self):
        return {"status": "scheduled", "marked_messages": 25, "deleted_matches": 3, "job_id": 3}

    def request_cluster_refresh(self, embedding_model):
        return {"status": "scheduled", "embedding_model": embedding_model, "job_id": 2}

    def active_model_counts(self):
        return {}

    def active_search_stage_counts(self):
        return {}


class FakeSmartSearchRuntime:
    def register_queue_handlers(self, _runtime):
        pass

    def __init__(self, _settings, _llm_service):
        pass

    def start(self):
        return None

    def stop(self):
        return None


def build_fake_app(monkeypatch, settings=None):
    fake_repository = FakeRepository(SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(api, "connect_database", lambda _url: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(api, "init_database", lambda _connection: None)
    monkeypatch.setattr(api, "Repository", lambda _connection: fake_repository)
    monkeypatch.setattr(api, "ConnectorRegistry", FakeConnectorRegistry)
    monkeypatch.setattr(api, "ChatService", FakeChatService)
    monkeypatch.setattr(api, "PipelineRuntime", FakePipelineRuntime)
    monkeypatch.setattr(api, "SmartSearchRuntime", FakeSmartSearchRuntime)
    monkeypatch.setattr(api.WorkQueueRuntime, "refresh_presets",
                        lambda _runtime: setattr(fake_repository, "refresh_calls", fake_repository.refresh_calls + 1))
    monkeypatch.setattr(api, "get_settings", lambda: settings or AppSettings(database_url="postgresql://test/mailintel"))
    return api.create_app()


def route_endpoint(app, name):
    for route in app.routes:
        if route.name == name:
            return route.endpoint
    raise AssertionError(f"route {name} not found")


def test_queue_controls_validate_list_and_cancel_deleted_search(monkeypatch):
    from mailintel.api.schemas import QueuePriorityPayload
    from mailintel.services.llm import LLMService

    cancelled = []
    monkeypatch.setattr(LLMService, "cancel", lambda self, key: cancelled.append(key))
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    row = {"id": 42, "kind": "search_compress", "state": "processing", "priority": 1,
           "payload": {"search_id": 9, "private_content": "not returned"}}
    repository.queue_list = lambda **kwargs: {"jobs": [dict(row, payload=None)], "total": 1, **kwargs}
    repository.queue_update_priority = lambda job_id, priority: {**row, "priority": priority} if job_id == 42 else None
    repository.queue_pause = lambda job_id: {**row, "state": "paused"} if job_id == 42 else None
    repository.queue_resume = lambda job_id: {**row, "state": "new"} if job_id == 42 else None
    repository.queue_delete = lambda job_id: row if job_id == 42 else None
    repository.transaction = nullcontext
    search_updates = []
    repository.cancel_smart_search_job = lambda search_id: search_updates.append(("cancel", search_id))
    repository.queue_cancel_prefix = lambda prefix, **kwargs: search_updates.append(("prefix", prefix))
    repository._execute = lambda query, values: search_updates.append(("clear", values))

    with pytest.raises(HTTPException) as invalid:
        route_endpoint(app, "list_queue")(state="done")
    assert invalid.value.status_code == 400
    page = route_endpoint(app, "list_queue")(state="paused", limit=1000, offset=-1)
    assert page["limit"] == 500 and page["offset"] == 0
    searched = route_endpoint(app, "list_queue")(q="  renewal  ", kind="embed_message", state="paused")
    assert searched["q"] == "renewal" and searched["kind"] == "embed_message"
    assert searched["state"] == "paused"
    result = route_endpoint(app, "reprioritize_queue_job")(42, QueuePriorityPayload(priority=0))
    assert result["job"]["priority"] == 0 and "payload" not in result["job"]
    assert route_endpoint(app, "pause_queue_job")(42)["job"]["state"] == "paused"
    assert route_endpoint(app, "resume_queue_job")(42)["job"]["state"] == "new"
    assert "payload" not in route_endpoint(app, "delete_queue_job")(42)["job"]
    assert cancelled == ["job:42", "job:42", "search:9"]
    assert search_updates == [("cancel", 9), ("prefix", "search:9:"), ("clear", (9,))]
    with pytest.raises(HTTPException) as missing:
        route_endpoint(app, "pause_queue_job")(99)
    assert missing.value.status_code == 404
    assert cancelled == ["job:42", "job:42", "search:9"]


def test_startup_discards_unfinished_smart_searches(monkeypatch):
    build_fake_app(monkeypatch)

    assert FakeRepository.instance.startup_calls == [
        "discard_unfinished_smart_searches",
        "mark_interrupted_jobs",
    ]


def test_smart_search_history_lists_metadata_and_removes_only_finished_jobs(monkeypatch):
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    calls = []
    repository.list_smart_search_jobs = lambda **kwargs: [
        {"job_id": 12, "question": "flight booking", "status": "succeeded", "retryable": False},
        {"job_id": 11, "question": "old flight", "status": "failed", "retryable": True},
    ]
    page = route_endpoint(app, "list_searches")(limit=1, before_id=None)
    assert page == {
        "searches": [{"job_id": 12, "question": "flight booking", "status": "succeeded", "retryable": False}],
        "next_before_id": 12,
    }
    with pytest.raises(HTTPException) as invalid:
        route_endpoint(app, "list_searches")(limit=101, before_id=None)
    assert invalid.value.status_code == 400

    repository.get_smart_search_job = lambda search_id: {"job_id": search_id} if search_id != 99 else None
    repository.delete_smart_search_job = lambda search_id: calls.append(search_id) or search_id != 13
    assert route_endpoint(app, "delete_search")(12) == {"removed": True}
    assert calls == [12]
    with pytest.raises(HTTPException) as active:
        route_endpoint(app, "delete_search")(13)
    assert active.value.status_code == 409
    with pytest.raises(HTTPException) as missing:
        route_endpoint(app, "delete_search")(99)
    assert missing.value.status_code == 404


def test_restored_connector_helpers_back_discovery_sync_maintenance_and_chat(monkeypatch):
    app = build_fake_app(monkeypatch)

    discover = route_endpoint(app, "discover_mailboxes")(1)
    assert any(mailbox["provider_mailbox_id"] == "Archive" for mailbox in discover["mailboxes"])

    sync = route_endpoint(app, "sync_account")(1)
    assert sync["scheduled_mailboxes"] == 1

    reset = route_endpoint(app, "reset_processed_emails")({"source_ids": [1]})
    assert reset["sync_results"][0]["status"] == "scheduled"

    chat = route_endpoint(app, "chat")({"query": "fetch context"})
    assert "live_fetch_used" not in chat
    assert chat["sources"] == []
    assert any(getattr(route, "path", "") == "/api/search" for route in app.routes)


def test_llm_pause_routes_report_and_toggle_process_state(monkeypatch):
    app = build_fake_app(monkeypatch, AppSettings(database_url="postgresql://test/mailintel", llm_paused=True))

    from mailintel.services.llm import LLMService, llm_pause_controller
    calls = []
    repository = FakeRepository.instance
    repository.transaction = nullcontext
    repository._queue_lock = lambda: calls.append("lock")
    repository.queue_interrupt_presets = lambda reason: calls.append(("requeue", llm_pause_controller.is_paused(), reason))
    monkeypatch.setattr(LLMService, "cancel", lambda self, key=None: calls.append(("cancel", key)))
    get_pause = route_endpoint(app, "get_llm_pause")
    toggle_pause = route_endpoint(app, "toggle_llm_pause")

    assert get_pause() == {"paused": True}
    assert toggle_pause() == {"paused": False}
    assert get_pause() == {"paused": False}
    assert calls == ["lock"]
    assert toggle_pause() == {"paused": True}
    assert calls == ["lock", "lock", ("requeue", True, "Interrupted by global AI pause"), ("cancel", None)]
    assert toggle_pause() == {"paused": False}
    assert calls[-1] == "lock"


def test_api_requires_bearer_token_and_sets_security_headers(monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    app = build_fake_app(monkeypatch, AppSettings(database_url="postgresql://test/mailintel", api_token="secret"))
    client = TestClient(app)

    public_response = client.get("/api/health")
    assert public_response.status_code == 200
    assert public_response.headers["x-content-type-options"] == "nosniff"
    assert public_response.headers["x-frame-options"] == "DENY"

    rejected = client.get("/api/accounts")
    assert rejected.status_code == 401

    accepted = client.get("/api/accounts", headers={"Authorization": "Bearer secret"})
    assert accepted.status_code == 200
    assert accepted.json()["accounts"]


def test_api_routes_receive_capability_metadata(monkeypatch):
    app = build_fake_app(monkeypatch)
    sync = route_endpoint(app, "sync_account")
    reset = route_endpoint(app, "reset_processed_emails")
    accounts = route_endpoint(app, "list_accounts")

    assert sync.mailintel_capability == "sync.run"
    assert reset.mailintel_capability == "maintenance.destroy"
    assert accounts.mailintel_capability == "mail.read"


def test_all_private_api_routes_declare_capabilities(monkeypatch):
    app = build_fake_app(monkeypatch)

    missing = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path == "/api/health":
            continue
        if not getattr(route.endpoint, "mailintel_capability", None):
            missing.append(path)

    assert missing == []
    assert route_endpoint(app, "save_llm_preset").mailintel_audit_event == "llm_preset.saved"
    assert route_endpoint(app, "export_portable_config").mailintel_audit_event == "config.exported"


def test_status_endpoint_aggregates_processing_and_mailbox_progress(monkeypatch):
    app = build_fake_app(monkeypatch)
    save = route_endpoint(app, "save_llm_preset")
    save(
        {
            "name": "Local Ollama",
            "provider": "ollama",
            "enabled": True,
            "priority": 10,
            "endpoint_url": "http://127.0.0.1:11434",
            "text_model": "summary",
            "embedding_model": "embed",
        }
    )

    response = route_endpoint(app, "get_status")()

    assert response["embedding_model"] == "embed"
    assert response["analyses"] == {"done": 43, "total": 110, "in_progress": 2, "queued": 65, "retrying": 0}
    assert response["embeddings"] == {
        "done": 35,
        "total": 116,
        "in_progress": 1,
        "queued": 5,
        "retrying": 0,
        "claimable": 2,
        "waiting_for_analysis": 75,
    }
    assert response["message_embeddings"] == {
        "done": 31,
        "total": 110,
        "in_progress": 1,
        "queued": 3,
        "retrying": 0,
        "claimable": 2,
        "waiting_for_analysis": 75,
    }
    assert response["interval_summary_embeddings"] == {
        "done": 4,
        "total": 6,
        "in_progress": 0,
        "queued": 2,
        "retrying": 0,
        "claimable": 0,
        "waiting_for_analysis": 0,
    }
    assert response["clustering"] == {"done": 18, "total": 31, "in_progress": 1, "queued": 12, "retrying": 0, "status": "running", "description": ""}
    assert response["summaries"] == {"done": 2, "total": 7, "running": 1, "in_progress": 1, "queued": 4, "retrying": 0, "status": "running"}
    assert response["search_activity"] == {"active": False, "stages": {}, "label": ""}
    assert response["mailboxes"][0]["crawl_status"] == "running"
    assert response["mailboxes"][0]["crawl_done"] == 9
    assert response["mailboxes"][0]["crawl_total"] == 24
    assert response["mailboxes"][0]["ingesting_done"] == 43
    assert response["mailboxes"][0]["ingesting_total"] == 110


def test_status_endpoint_marks_equal_priority_presets_selected(monkeypatch):
    app = build_fake_app(monkeypatch)
    save = route_endpoint(app, "save_llm_preset")
    for name in ("DGX 1", "DGX 2"):
        save(
            {
                "name": name,
                "provider": "ollama",
                "enabled": True,
                "priority": 10,
                "endpoint_url": "http://127.0.0.1:11434",
                "text_model": "summary",
                "embedding_model": "embed",
            }
        )

    FakeRepository.instance.queue_activity_rows = {
        "jobs": [
            {"kind": "analyze_message", "state": "processing", "preset_id": 1, "worker_id": "preset:1:2"},
            {"kind": "embed_message", "state": "processing", "preset_id": 2, "worker_id": "preset:2:1"},
        ],
        "pending": [
            {"kind": "analyze_message", "target_preset_id": 1, "embedding_model": "", "count": 3},
            {"kind": "analyze_message", "target_preset_id": None, "embedding_model": "", "count": 2},
            {"kind": "embed_message", "target_preset_id": None, "embedding_model": "other-model", "count": 100},
        ],
    }
    FakeRepository.instance.llm_presets[0]['concurrency'] = 3
    response = route_endpoint(app, "get_status")()

    generation_models = [model for model in response["models"] if model["operation"] == "generation"]
    assert {model["preset_name"] for model in generation_models if model["routing_status"] == "selected"} == {
        "DGX 1",
        "DGX 2",
    }

    by_slot = {(model["preset_name"], model["operation"]): model for model in response["models"]}
    assert by_slot[("DGX 1", "generation")]["active_count"] == 1
    assert [slot['state'] for slot in by_slot[("DGX 1", "generation")]['slots']] == [
        'healthy_idle', 'active_pipeline', 'healthy_idle',
    ]
    assert by_slot[("DGX 1", "generation")]["queued_count"] == 5
    assert by_slot[("DGX 2", "generation")]["active_count"] == 0
    assert by_slot[("DGX 2", "generation")]["queued_count"] == 2
    assert by_slot[("DGX 2", "embedding")]["active_count"] == 1
    assert by_slot[("DGX 2", "embedding")]["queued_count"] == 0
    assert all("preset_statuses" not in model for model in response["models"])

    # Retained capability flags cannot manufacture an LED without its model.
    FakeRepository.instance.llm_presets[0].update(text_model="")
    FakeRepository.instance.llm_presets[1]["embedding_model"] = ""
    response = route_endpoint(app, "get_status")()
    assert {(model["preset_name"], model["operation"]) for model in response["models"]} == {
        ("DGX 1", "embedding"), ("DGX 2", "generation"),
    }


def test_status_endpoint_does_not_reuse_stored_messages_as_crawl_progress(monkeypatch):
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    repository.pipeline_mailbox_statuses[0] = {
        **repository.pipeline_mailbox_statuses[0],
        "sync_status": "completed",
        "crawl_status": "completed",
        "crawl_job_status": "completed",
        "crawl_total_count": 287,
        "crawl_processed_count": 287,
        "total_messages": 110,
    }
    save = route_endpoint(app, "save_llm_preset")
    save(
        {
            "name": "Local Ollama",
            "provider": "ollama",
            "enabled": True,
            "priority": 10,
            "endpoint_url": "http://127.0.0.1:11434",
            "text_model": "summary",
            "embedding_model": "embed",
        }
    )

    response = route_endpoint(app, "get_status")()

    assert response["mailboxes"][0]["total_messages"] == 110
    assert response["mailboxes"][0]["crawl_status"] == "completed"
    assert response["mailboxes"][0]["crawl_done"] == 0
    assert response["mailboxes"][0]["crawl_total"] == 0


def test_disabled_account_manual_sync_returns_clear_400(monkeypatch):
    app = build_fake_app(monkeypatch)

    try:
        route_endpoint(app, "sync_account")(2)
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "disabled" in exc.detail
    else:
        raise AssertionError("disabled account sync should fail")


def test_rebuild_interest_matches_endpoint(monkeypatch):
    app = build_fake_app(monkeypatch)

    response = route_endpoint(app, "rebuild_interest_matches")({"limit": 25})

    assert response == {
        "status": "scheduled",
        "rebuilt_messages": 25,
        "marked_messages": 25,
        "deleted_matches": 3,
        "job_id": 3,
    }


def test_llm_preset_api_defaults_and_round_trips_text_context_window(monkeypatch):
    app = build_fake_app(monkeypatch)

    save = route_endpoint(app, "save_llm_preset")
    list_presets = route_endpoint(app, "list_llm_presets")

    created = save(
        {
            "name": "Local Ollama",
            "provider": "ollama",
            "enabled": True,
            "priority": 10,
            "endpoint_url": "http://127.0.0.1:11434",
            "text_model": "summary",
            "embedding_model": "embed",
        }
    )
    presets = list_presets()["llm_presets"]

    assert created["llm_preset_id"] == 1
    assert presets[0]["text_context_window"] == 8192
    assert presets[0]["embedding_context_window"] == 8192

    updated = save(
        {
            "id": 1,
            "name": "Local Ollama",
            "provider": "ollama",
            "enabled": True,
            "priority": 10,
            "endpoint_url": "http://127.0.0.1:11434",
            "text_model": "summary",
            "embedding_model": "embed",
            "text_context_window": 4096,
            "embedding_context_window": 12288,
        }
    )
    presets = list_presets()["llm_presets"]

    assert updated["llm_preset_id"] == 1
    assert presets[0]["text_context_window"] == 4096
    assert presets[0]["embedding_context_window"] == 12288
    assert FakeRepository.instance.refresh_calls == 2


def test_pipeline_settings_round_trip(monkeypatch):
    app = build_fake_app(monkeypatch)
    save = route_endpoint(app, "save_pipeline_settings")
    save({"canonical_embedding_model": "embed-v2"})
    response = route_endpoint(app, "get_pipeline_settings")()
    assert response["pipeline_settings"] == {"canonical_embedding_model": "embed-v2"}
    assert FakeRepository.instance.refresh_calls == 1


def test_maintenance_rebuild_topics_endpoint(monkeypatch):
    app = build_fake_app(monkeypatch)
    save = route_endpoint(app, "save_llm_preset")
    save(
        {
            "name": "Local Ollama",
            "provider": "ollama",
            "enabled": True,
            "priority": 10,
            "endpoint_url": "http://127.0.0.1:11434",
            "text_model": "summary",
            "embedding_model": "embed",
        }
    )

    response = route_endpoint(app, "maintenance_rebuild_topics")()

    assert response == {
        "status": "ok",
        "rebuild": {"status": "scheduled", "embedding_model": "embed", "job_id": 2},
    }


def test_maintenance_reset_summaries_endpoint(monkeypatch):
    app = build_fake_app(monkeypatch)

    response = route_endpoint(app, "reset_interval_summaries")()

    assert response == {"status": "ok", "deleted_summaries": 7}


def test_summary_rebuild_endpoint_requests_selected_interval(monkeypatch):
    app = build_fake_app(monkeypatch)

    response = route_endpoint(app, "request_summary_rebuild")(
        {"start_date": "2026-05-11", "duration": "week"}
    )

    assert response == {"status": "ok", "requested": 2, "summary_ids": [701, 702]}
    assert FakeRepository.instance.requested_interval_rebuilds == [(date(2026, 5, 11), 7, "weekly")]


def test_summary_rebuild_endpoint_validates_week_start(monkeypatch):
    app = build_fake_app(monkeypatch)

    try:
        route_endpoint(app, "request_summary_rebuild")({"start_date": "2026-05-12", "duration": "week"})
    except HTTPException as exc:
        assert exc.status_code == 400
        assert exc.detail == "week summaries must start on Monday"
    else:
        raise AssertionError("expected HTTPException")


def test_maintenance_rebuild_topics_requires_preset(monkeypatch):
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    repository.pipeline_settings["canonical_embedding_model"] = ""

    try:
        route_endpoint(app, "maintenance_rebuild_topics")()
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "No canonical embedding model" in exc.detail
    else:
        raise AssertionError("maintenance topic rebuild should require a canonical embedding model")


def test_topics_are_loaded_separately_from_triage(monkeypatch):
    app = build_fake_app(monkeypatch)
    triage = route_endpoint(app, "triage")()
    assert "topics" not in triage
    topics = route_endpoint(app, "list_topics")()["topics"]
    assert topics == [{"id": 1, "name": "Security Incident", "message_count": 1}]
    detail = route_endpoint(app, "topic_detail")(1)["topic"]
    assert detail["name"] == "Security Incident"
    assert "matched_messages" not in detail


def test_topics_date_range_and_messages(monkeypatch):
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    repository.topic_categories[1]["matched_messages"] = [
        {"id": 501, "subject": "Current incident", "received_at": "2026-05-10T12:00:00+00:00"},
        {"id": 502, "subject": "Old incident", "received_at": "2026-04-22T12:00:00+00:00"},
    ]
    topics = route_endpoint(app, "list_topics")(from_date="2026-05-01", to_date="2026-05-31")["topics"]
    assert topics[0]["message_count"] == 1
    messages = route_endpoint(app, "topic_messages")(1, from_date="2026-05-01", to_date="2026-05-31")
    assert [row["subject"] for row in messages["messages"]] == ["Current incident"]


def test_triage_all_time_is_unbounded_when_dates_are_missing(monkeypatch):
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    repository.triage_rows = [
        {"id": 1, "subject": "Dated priority", "received_at": "2026-05-10T10:00:00+00:00"},
        {"id": 2, "subject": "No date priority", "received_at": None},
    ]

    response = route_endpoint(app, "triage")()

    assert [row["subject"] for row in response["triage"]] == ["Dated priority", "No date priority"]


def test_triage_rejects_invalid_date_range(monkeypatch):
    app = build_fake_app(monkeypatch)

    try:
        route_endpoint(app, "triage")(from_date="2026/05/01")
    except HTTPException as exc:
        assert exc.status_code == 400
        assert "from_date must be YYYY-MM-DD" in exc.detail
    else:
        raise AssertionError("triage should reject invalid from_date")


def test_topic_rename_and_ai_relabel_routes(monkeypatch):
    app = build_fake_app(monkeypatch)
    repository = FakeRepository.instance
    renamed = route_endpoint(app, "rename_topic")(1, {"name": "Security Response"})
    assert renamed["topic"]["name"] == "Security Response"
    queued = route_endpoint(app, "request_topic_relabel")(1)
    assert queued == {"status": "queued", "job_id": 55}
    assert repository.enqueued_topic_job == (
        "relabel_topic", "topic-relabel:1",
        {"topic_id": 1, "expected_name": "Security Response", "embedding_model": "embed"}, 1,
    )


def test_cooldown_recovery_interval_prefers_new_env_name(monkeypatch):
    monkeypatch.setenv("MAILINTEL_PRESET_PROBE_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("MAILINTEL_PRESET_COOLDOWN_RECOVERY_INTERVAL_SECONDS", "5")

    settings = AppSettings()

    assert settings.preset_cooldown_recovery_interval_seconds == 5
