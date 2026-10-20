# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..account_secrets import account_for_connector
from ..connectors.base import ConnectorAuthenticationError, ConnectorConnectionError, ConnectorError, ReadOnlyMailConnector
from ..connectors.registry import ConnectorRegistry
from ..db import connect_database
from ..models import (
    InterestProfile,
    JobRecord,
    Mailbox,
    NormalizedMessage,
)
from ..repositories import Repository
from ..secrets import PasswordEncryptor
from ..settings import AppSettings
from .classification import build_summary, match_interest_profiles
from .llm import LLMInputError, LLMService
from .topics import INCREMENTAL_CLUSTERING_ALGORITHM_VERSION


logger = logging.getLogger(__name__)

MAILBOX_NO_NEW_MAIL_RETRY_SECONDS = 30
INTERVAL_SUMMARY_INPUT_BATCH_SIZE = 32
INTERVAL_SUMMARY_STALE_DAILY_LIMIT = 1000
INTERVAL_SUMMARY_MERGE_FALLBACK_WARNING = (
    "Automatic merge of batched summaries failed; showing unmerged batch summaries until retry succeeds."
)
STALE_PROBING_AFTER_SECONDS = 600
MAILBOX_UNREACHABLE_RETRY_SECONDS = (1, 1, 10, 20)
MAILBOX_UNREACHABLE_STEADY_RETRY_SECONDS = 60


def _timezone_or_utc(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        logger.warning("invalid summary timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")


def _safe_timezone_name(name: str) -> str:
    return _timezone_or_utc(name).key


def _hash_step_input(*parts: str) -> str:
    payload = "\n".join(parts)
    return sha256(payload.encode("utf-8")).hexdigest()


def _row_to_interest_profile(row: object) -> InterestProfile:
    return InterestProfile(
        id=int(row["id"]),
        name=row["name"],
        description=row["description"],
        collaborators=json.loads(row["collaborators_json"])
        if isinstance(row["collaborators_json"], str)
        else list(row["collaborators_json"]),
        signals=row["signals"],
        priority_weight=float(row["priority_weight"]),
        dashboard_prominence=int(row["dashboard_prominence"]),
        summary_detail=row["summary_detail"],
        watch_mode=row["watch_mode"],
    )


def _row_to_message(row: dict[str, Any]) -> NormalizedMessage:
    recipients = row.get("recipients_json", [])
    cc = row.get("cc_json", [])
    if isinstance(recipients, str):
        recipients = json.loads(recipients or "[]")
    if isinstance(cc, str):
        cc = json.loads(cc or "[]")
    return NormalizedMessage(
        provider_message_id=str(row["provider_message_id"]),
        mailbox_provider_id=str(row.get("provider_mailbox_id") or ""),
        internet_message_id=row.get("internet_message_id"),
        thread_id=row.get("thread_id"),
        subject=str(row.get("subject") or ""),
        sender_name=str(row.get("sender_name") or ""),
        sender_address=str(row.get("sender_address") or ""),
        recipients=[str(item) for item in recipients],
        cc=[str(item) for item in cc],
        received_at=row.get("received_at"),
        sent_at=row.get("sent_at"),
        unread=not bool(row.get("is_read")),
        has_attachments=bool(row.get("has_attachments")),
        importance_hint=row.get("importance_hint"),
        snippet_hash=str(row.get("snippet_hash") or ""),
        metadata={},
    )


class PipelineRuntime:
    """Source ownership and concrete queue handlers; LLM capacity belongs to presets."""
    def __init__(self, settings, password_encryptor, connector_registry=None, llm_service=None):
        self.settings = settings
        self.password_encryptor = password_encryptor
        self.connector_registry = connector_registry or ConnectorRegistry()
        self.llm_service = llm_service or LLMService()
        self._stop_event = threading.Event()
        self._source_threads = {}
        self._source_wake = {}
        self._started = False
        self._next_interval_discovery = 0.0

    def register_queue_handlers(self, runtime):
        from .interval_queue import IntervalQueue
        from . import cluster_queue
        self.queue_runtime = runtime
        self.interval_queue = IntervalQueue(self)
        self.cluster_queue = cluster_queue
        runtime.register('analyze_message', self._handle_analysis, capability='mail_summary')
        runtime.register('embed_message', self._handle_embedding, operation='embedding', model='embedding', capability='mail_embedding')
        runtime.register('embed_interval', self._handle_interval_embedding, operation='embedding', model='embedding', capability='interval_embedding')
        self.interval_queue.register(runtime)
        self.cluster_queue.register(runtime,self.llm_service)
        runtime.register_producer(self._discover_work)

    def start(self):
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        repository = self._repository()
        try:
            repository._execute("UPDATE mailboxes SET crawl_status='pending', crawl_lease_expires_at=NULL WHERE crawl_status='running'")
            repository.commit()
            canonical = str(repository.get_pipeline_settings().get('canonical_embedding_model') or '')
            if canonical:
                repository.restore_current_message_embeddings(canonical)
                repository.initialize_incremental_clustering(canonical,INCREMENTAL_CLUSTERING_ALGORITHM_VERSION)
            self._reconcile_sources(repository)
        finally:
            repository.connection.close()

    def stop(self, timeout=5.0):
        self._started = False
        self._stop_event.set()
        for wake in self._source_wake.values():
            wake.set()
        for thread in list(self._source_threads.values()):
            thread.join(timeout=timeout)
        self._source_threads.clear()

    def _reconcile_sources(self, repository):
        if not self._started:
            return
        for account in repository.list_mail_accounts():
            account_id = int(account['id'])
            thread = self._source_threads.get(account_id)
            if account['status'] == 'active' and (thread is None or not thread.is_alive()):
                self._source_wake.setdefault(account_id,threading.Event())
                thread = threading.Thread(target=self._source_loop, args=(account_id,), name=f'source-{account_id}', daemon=True)
                self._source_threads[account_id] = thread
                thread.start()

    def _source_loop(self, account_id):
        worker_id = f'source:{account_id}'
        repository = self._repository()
        try:
            repository.queue_set_worker(worker_id,'online',source_id=account_id)
        finally:
            repository.connection.close()
        auth_failed_config = None
        while not self._stop_event.is_set():
            repository = self._repository()
            try:
                account = repository.get_mail_account(account_id)
                if account is None or account['status'] != 'active':
                    break
                config = json.dumps(dict(account),sort_keys=True,default=str)
                if auth_failed_config == config and not self._source_wake[account_id].is_set():
                    repository.rollback()
                    self._stop_event.wait(self.settings.worker_poll_seconds)
                    continue
                self._source_wake[account_id].clear()
                auth_failed_config = None
                if not repository.list_mailboxes(account_id=account_id):
                    connector = self._create_connector(account)
                    repository.rollback()
                    mailboxes = connector.list_mailboxes()
                    repository.queue_set_worker(worker_id,'online',source_id=account_id)
                    for mailbox in mailboxes:
                        mailbox.account_id = account_id
                        repository.upsert_mailbox(mailbox)
                rows = list(repository._execute("""SELECT * FROM mailboxes WHERE account_id=%s AND monitored
                  AND (crawl_status <> 'failed' OR next_sync_at IS NOT NULL)
                  AND COALESCE(next_sync_at,now()) <= now() ORDER BY COALESCE(next_sync_at,now()),id""", (account_id,)))
                repository.rollback()
                for row in rows:
                    if self._stop_event.is_set():
                        break
                    repository._execute("UPDATE mailboxes SET crawl_status='running',crawl_attempt_count=crawl_attempt_count+1,crawl_last_error='',crawl_lease_expires_at=NULL WHERE id=%s", (row['id'],))
                    repository.commit()
                    row['crawl_attempt_count'] = int(row.get('crawl_attempt_count') or 0) + 1
                    self._process_mailbox(repository, row, '')
            except Exception as exc:
                if isinstance(exc,ConnectorAuthenticationError):
                    auth_failed_config = config
                repository.rollback()
                repository.queue_set_worker(worker_id, 'unavailable', source_id=account_id, reason=str(exc))
            finally:
                repository.connection.close()
            self._sleep(self.settings.worker_poll_seconds)
        repository = self._repository()
        try:
            repository.queue_set_worker(worker_id, 'stopped', source_id=account_id)
        finally:
            repository.connection.close()

    def _discover_work(self, repository):
        self._reconcile_sources(repository)
        canonical = str(repository.get_pipeline_settings().get('canonical_embedding_model') or '')
        config_revision = self._analysis_config_revision(repository)
        rows = list(repository._execute("""SELECT ps.message_id, ps.source_input_hash FROM message_processing_state ps
          JOIN message_body_cache bc ON bc.message_id=ps.message_id
          WHERE (ps.summary_status <> 'done' OR ps.interest_status <> 'done' OR ps.summary_needs_refresh)
          AND NOT EXISTS (SELECT 1 FROM work_queue q WHERE q.dedup_key='analysis:'||ps.message_id
            AND q.payload->>'source_input_hash'=ps.source_input_hash
            AND q.payload->>'config_revision'=%s)
          ORDER BY ps.updated_at,ps.message_id LIMIT 25""", (config_revision,)))
        for row in rows:
            self._enqueue_analysis(repository, int(row['message_id']),config_revision)
        if canonical:
            rows = list(repository._execute("""SELECT ps.message_id FROM message_processing_state ps
              WHERE ps.summary_status='done' AND (ps.embedding_status <> 'done' OR ps.embedding_needs_refresh OR ps.embedding_model<>%s)
              AND NOT EXISTS (SELECT 1 FROM work_queue q WHERE q.dedup_key='embedding:'||ps.message_id
                AND q.payload->>'source_input_hash'=ps.source_input_hash AND q.payload->>'embedding_model'=%s)
              ORDER BY ps.updated_at,ps.message_id LIMIT 25""", (canonical,canonical)))
            for row in rows:
                self._enqueue_embedding(repository,int(row['message_id']),canonical)
            self.cluster_queue.discover(repository,canonical)
        if time.monotonic() >= self._next_interval_discovery:
            self.interval_queue.discover(repository)
            self._next_interval_discovery = time.monotonic() + self.settings.interval_summary_retry_seconds

    def _analysis_config_revision(self, repository):
        return _hash_step_input(json.dumps(repository.list_interest_profiles(),sort_keys=True,default=str),
            self._summary_user_context(repository))

    def _enqueue_analysis(self, repository, message_id,config_revision=None):
        state = repository.get_message_processing_state(message_id)
        if state:
            repository.queue_enqueue('analyze_message', f'analysis:{message_id}',
                {'message_id':message_id,'source_input_hash':state['source_input_hash'],
                 'config_revision':config_revision or self._analysis_config_revision(repository)}, priority=2)

    def _enqueue_embedding(self, repository, message_id, canonical):
        state = repository.get_message_processing_state(message_id)
        if state and canonical:
            row = repository.get_message_detail(message_id)
            if not row:
                return
            input_hash = repository.embedding_input_hash(repository.embedding_input_for_row(row))
            repository.queue_enqueue('embed_message', f'embedding:{message_id}',
                {'message_id':message_id,'source_input_hash':state['source_input_hash'],
                 'input_hash':input_hash,'embedding_model':canonical}, priority=2)

    def _handle_analysis(self, repository, job, preset):
        message_id = int(job['payload']['message_id'])
        row = repository.get_analysis_message_detail(message_id)
        if row is None:
            return None
        message = _row_to_message(row)
        body = str(row.get('body_text') or '')
        profile_rows = repository.list_interest_profiles()
        profiles = [_row_to_interest_profile(item) for item in profile_rows]
        matches = match_interest_profiles(message,body,profiles)
        fallback = build_summary(message,body,matches)
        context = self._summary_user_context(repository)
        config_revision = _hash_step_input(json.dumps(profile_rows,sort_keys=True,default=str),context)
        repository.rollback()
        try:
            analysis = self.llm_service.analyze_message(preset,message=message,body_text=body,profiles=profiles,
                fallback=fallback,priority=job['priority'],client_id='analysis',user_context=context)
            summary, matches = analysis.summary, analysis.interest_matches
        except LLMInputError:
            summary = fallback
        summary.message_id = message_id
        input_hash = _hash_step_input(message.subject,message.sender_name,message.sender_address,body)
        def apply():
            state = repository._execute('SELECT * FROM message_processing_state WHERE message_id=%s FOR UPDATE', (message_id,)).fetchone()
            if not state:
                return
            if state['source_input_hash'] != job['payload']['source_input_hash'] or self._analysis_config_revision(repository) != config_revision:
                self._enqueue_analysis(repository,message_id)
                return
            repository.replace_interest_matches(message_id,matches)
            repository.upsert_summary(summary)
            repository.complete_analysis(message_id,interest_input_hash=input_hash,
                summary_input_hash=_hash_step_input(input_hash,context,summary.summary_text,summary.rationale),
                preset_name=preset.name,summary_needs_refresh=False)
            canonical = str(repository.get_pipeline_settings().get('canonical_embedding_model') or '')
            self._enqueue_embedding(repository,message_id,canonical)
        return apply

    def _handle_embedding(self, repository, job, preset):
        message_id = int(job['payload']['message_id'])
        row = repository.get_message_detail(message_id)
        if row is None:
            return None
        canonical = job['payload']['embedding_model']
        text = repository.embedding_input_for_row(row)
        repository.rollback()
        vectors = self.llm_service.embed_texts(preset,texts=[text],priority=job['priority'],client_id='embedding',raise_on_error=True)
        if not vectors:
            raise RuntimeError('Embedding provider returned no vector')
        def apply():
            state = repository.get_message_processing_state(message_id)
            configured = str(repository.get_pipeline_settings().get('canonical_embedding_model') or '')
            if not state or state['source_input_hash'] != job['payload']['source_input_hash']:
                return
            latest = repository.get_message_detail(message_id)
            if latest and repository.embedding_input_for_row(latest) != text:
                self._enqueue_embedding(repository,message_id,configured)
                return
            if configured != canonical:
                self._enqueue_embedding(repository,message_id,configured)
                return
            repository.upsert_message_embedding(message_id=message_id,preset_name=preset.name,
                embedding_model=canonical,input_text=text,vector=vectors[0])
            repository.complete_embedding(message_id,input_text=text,embedding_model=canonical,preset_name=preset.name)
            self.cluster_queue.enqueue(repository,message_id,canonical,repository.embedding_input_hash(text))
        return apply

    def _handle_interval_embedding(self, repository, job, preset):
        summary_id = int(job['payload']['summary_id'])
        row = repository.get_interval_summary_embedding_detail(summary_id)
        if row is None:
            return None
        canonical = job['payload']['embedding_model']
        text = repository.interval_summary_embedding_input_for_row(row)
        repository.rollback()
        vectors = self.llm_service.embed_texts(preset,texts=[text],priority=job['priority'],client_id='embedding',raise_on_error=True)
        if not vectors:
            raise RuntimeError('Embedding provider returned no vector')
        def apply():
            configured = str(repository.get_pipeline_settings().get('canonical_embedding_model') or '')
            latest = repository.get_interval_summary_embedding_detail(summary_id)
            if not latest:
                return
            if configured != canonical or repository.interval_summary_embedding_input_for_row(latest) != text:
                if configured:
                    self.interval_queue._enqueue_embedding(repository,summary_id,configured)
                return
            repository.upsert_interval_summary_embedding(summary_id=summary_id,preset_name=preset.name,
                embedding_model=canonical,input_text=text,vector=vectors[0])
            repository.complete_interval_summary_embedding(summary_id,input_text=text,embedding_model=canonical,preset_name=preset.name)
        return apply

    def schedule_account_sync(self, account_id: int) -> dict[str, Any]:
        repository = self._repository()
        try:
            account = repository.get_mail_account(account_id)
            if account is None:
                raise LookupError(f"Account {account_id} not found")
            scheduled = repository.schedule_account_mailboxes(account_id)
            if account_id in self._source_wake:
                self._source_wake[account_id].set()
            job_id = repository.create_job(
                JobRecord(
                    id=None,
                    job_type="crawl",
                    status="completed",
                    phase="scheduled",
                    total_count=scheduled,
                    processed_count=0,
                    current_item=f"account:{account_id}",
                    metadata={"account_id": account_id},
                )
            )
            return {"status": "scheduled", "scheduled_mailboxes": scheduled, "job_id": job_id}
        finally:
            repository.connection.close()


    def request_embedding_refresh(self) -> dict[str, Any]:
        repository = self._repository()
        try:
            pipeline_settings = repository.get_pipeline_settings()
            canonical_model = str(pipeline_settings.get("canonical_embedding_model") or "")
            if not canonical_model:
                return {"status": "idle", "marked_messages": 0, "marked_interval_summaries": 0}
            search_docs = repository.rebuild_search_documents()
            marked_messages = repository.mark_embedding_refresh_pending(canonical_model)
            marked_summaries = repository.mark_interval_summary_embedding_refresh_pending(canonical_model)
            self._discover_work(repository)
            job_id = repository.create_job(
                JobRecord(
                    id=None,
                    job_type="embedding",
                    status="completed",
                    phase="scheduled",
                    total_count=marked_messages + marked_summaries,
                    processed_count=0,
                    current_item=canonical_model,
                    metadata={
                        "embedding_model": canonical_model,
                        "marked_messages": marked_messages,
                        "marked_interval_summaries": marked_summaries,
                        "rebuilt_search_documents": search_docs,
                    },
                )
            )
            return {
                "status": "scheduled",
                "marked_messages": marked_messages,
                "marked_interval_summaries": marked_summaries,
                "rebuilt_search_documents": search_docs,
                "job_id": job_id,
            }
        finally:
            repository.connection.close()


    def request_analysis_refresh(self) -> dict[str, Any]:
        repository = self._repository()
        try:
            result = repository.mark_analysis_refresh_pending()
            rows = list(repository._execute('SELECT message_id FROM message_processing_state'))
            for row in rows:
                state = repository.get_message_processing_state(int(row['message_id']))
                repository.queue_enqueue('analyze_message',f"analysis:{row['message_id']}",
                    {'message_id':int(row['message_id']),'source_input_hash':state['source_input_hash'],
                     'refresh_requested_at':datetime.now(UTC).isoformat(),
                     'config_revision':self._analysis_config_revision(repository)}, priority=2)
            job_id = repository.create_job(
                JobRecord(
                    id=None,
                    job_type="analysis",
                    status="completed",
                    phase="scheduled",
                    total_count=result["marked_messages"],
                    processed_count=0,
                    current_item="all",
                    metadata={"reason": "manual_analysis_refresh"},
                )
            )
            return {"status": "scheduled", **result, "job_id": job_id}
        finally:
            repository.connection.close()


    def request_cluster_refresh(self, embedding_model: str) -> dict[str, Any]:
        repository = self._repository()
        try:
            result = repository.reset_topic_rebuild_state(embedding_model)
            repository.initialize_incremental_clustering(embedding_model,INCREMENTAL_CLUSTERING_ALGORITHM_VERSION)
            self.cluster_queue.discover(repository, embedding_model)
            job_id = repository.create_job(
                JobRecord(
                    id=None,
                    job_type="clustering",
                    status="completed",
                    phase="scheduled",
                    total_count=result["ready_embeddings"],
                    processed_count=0,
                    current_item=embedding_model,
                    metadata={"embedding_model": embedding_model},
                )
            )
            return {"status": "scheduled", "embedding_model": embedding_model, **result, "job_id": job_id}
        finally:
            repository.connection.close()


    def _repository(self) -> Repository:
        connection = connect_database(self.settings.database_url)
        return Repository(connection)


    def _sleep(self, seconds: float) -> None:
        self._stop_event.wait(seconds)


    def _create_connector(self, account_row: object) -> ReadOnlyMailConnector:
        prepared = account_for_connector(dict(account_row), self.password_encryptor)
        return self.connector_registry.create(
            prepared["provider_type"],
            prepared["settings_json"],
            prepared["secret_locator"],
        )


    def _mailbox_unreachable_retry_seconds(self, crawl_attempt_count: int) -> int:
        index = max(crawl_attempt_count, 1) - 1
        if index < len(MAILBOX_UNREACHABLE_RETRY_SECONDS):
            return MAILBOX_UNREACHABLE_RETRY_SECONDS[index]
        return MAILBOX_UNREACHABLE_STEADY_RETRY_SECONDS


    def _mailbox_failure_retry_seconds(self, error: Exception, crawl_attempt_count: int) -> float | None:
        if isinstance(error, ConnectorAuthenticationError):
            return None
        if isinstance(error, (ConnectorConnectionError, OSError, TimeoutError)):
            return self._mailbox_unreachable_retry_seconds(crawl_attempt_count)
        if isinstance(error, ConnectorError):
            return self.settings.worker_poll_seconds
        return self.settings.worker_poll_seconds


    def _mailbox_retry_hint(self, retry_seconds: float) -> str:
        seconds = int(retry_seconds) if float(retry_seconds).is_integer() else retry_seconds
        unit = "second" if seconds == 1 else "seconds"
        return f"Automatic sync will retry in about {seconds} {unit}; you can retry manually sooner."


    def _mailbox_status_error_text(self, error: Exception, retry_seconds: float | None) -> str:
        message = error.operator_message if isinstance(error, ConnectorError) else str(error)
        if isinstance(error, ConnectorAuthenticationError):
            return f"{message} Automatic sync stopped for this mailbox; use the GUI retry button after fixing the credentials."
        if retry_seconds is not None:
            return f"{message} {self._mailbox_retry_hint(retry_seconds)}"
        return message


    def _process_mailbox(self, repository: Repository, mailbox_row: dict[str, Any], canonical_model: str) -> None:
        mailbox = Mailbox(
            id=int(mailbox_row["id"]),
            account_id=int(mailbox_row["account_id"]),
            provider_mailbox_id=str(mailbox_row["provider_mailbox_id"]),
            name=str(mailbox_row["name"]),
            monitored=bool(mailbox_row["monitored"]),
            sync_cursor=mailbox_row.get("sync_cursor"),
            last_synced_at=mailbox_row.get("last_synced_at"),
        )
        account = repository.get_mail_account(mailbox.account_id)
        if account is None:
            repository.release_mailbox_claim(mailbox.id or 0, status="failed", error_text="account not found")
            return
        job_id = repository.create_job(
            JobRecord(
                id=None,
                job_type="crawl",
                phase="fetching",
                current_item=mailbox.name,
                metadata={"account_id": mailbox.account_id, "mailbox_id": mailbox.id},
            )
        )
        try:
            connector = self._create_connector(account)
            settings_raw = account.get("settings_json") or {}
            if isinstance(settings_raw, str):
                settings_raw = json.loads(settings_raw)
            sync_window_days = settings_raw.get("sync_window_days")
            received_since = mailbox.last_synced_at
            if received_since is None and sync_window_days:
                received_since = datetime.now(UTC) - timedelta(days=int(sync_window_days))
            repository.rollback()
            refs = connector.list_message_refs(
                mailbox,
                cursor=mailbox.sync_cursor,
                unread_only=False,
                received_since=received_since,
            )
            refs_by_id = {ref.provider_message_id: ref for ref in refs}
            pending_ids = [
                ref.provider_message_id
                for ref in refs
                if (
                    not repository._execute("SELECT 1 FROM messages m JOIN message_body_cache bc ON bc.message_id=m.id WHERE m.mailbox_id=%s AND m.provider_message_id=%s", (mailbox.id,ref.provider_message_id)).fetchone()
                    or (
                        ref.received_at is not None
                        and repository.message_missing_received_at(
                            mailbox_id=mailbox.id or 0,
                            provider_message_id=ref.provider_message_id,
                        )
                    )
                )
            ]
            repository.update_job(job_id, total_count=len(pending_ids), current_item=mailbox.name)
            processed = 0
            for provider_message_id in pending_ids:
                repository.update_job(job_id, processed_count=processed, current_item=provider_message_id)
                repository.rollback()
                try:
                    bundle = connector.fetch_message_bundle(
                        mailbox,
                        provider_message_id,
                        max_chars=self.settings.body_segment_chars,
                    )
                except Exception as exc:
                    logger.warning("crawl skipping message %s: %s", provider_message_id, exc)
                    continue
                ref = refs_by_id.get(provider_message_id)
                if ref is not None:
                    if bundle.message.received_at is None:
                        bundle.message.received_at = ref.received_at
                    if bundle.message.sent_at is None:
                        bundle.message.sent_at = ref.received_at
                with repository.transaction():
                    repository._queue_lock()
                    message_id = repository.upsert_message_bundle(mailbox.id or 0, bundle)
                    self._enqueue_analysis(repository,message_id)
                processed += 1
            repository.update_mailbox_cursor(mailbox.id or 0, connector.get_sync_cursor(mailbox))
            next_sync_seconds = MAILBOX_NO_NEW_MAIL_RETRY_SECONDS if not pending_ids else self.settings.sync_interval_seconds
            repository.release_mailbox_claim(
                mailbox.id or 0,
                status="completed",
                next_sync_at=datetime.now(UTC) + timedelta(seconds=next_sync_seconds),
            )
            repository.update_job(job_id, status="completed", phase="completed", processed_count=processed)
            repository.queue_set_worker(f"source:{mailbox.account_id}","online",source_id=mailbox.account_id)
        except Exception as exc:
            retry_seconds = self._mailbox_failure_retry_seconds(exc, int(mailbox_row.get("crawl_attempt_count") or 1))
            next_sync_at = datetime.now(UTC) + timedelta(seconds=retry_seconds) if retry_seconds is not None else None
            error_text = self._mailbox_status_error_text(exc, retry_seconds)
            repository.release_mailbox_claim(
                mailbox.id or 0,
                status="failed",
                error_text=error_text,
                next_sync_at=next_sync_at,
                clear_next_sync_at=retry_seconds is None,
            )
            repository.update_job(job_id, status="failed", phase="failed", error_text=str(exc))
            repository.queue_set_worker(f"source:{mailbox.account_id}","unavailable",source_id=mailbox.account_id,reason=str(exc))


    def _summary_user_context(self, repository: Repository) -> str:
        if not hasattr(repository, "get_user_profile"):
            return ""
        try:
            row = repository.get_user_profile()
        except Exception:
            logger.debug("failed to load user profile for summary context", exc_info=True)
            return ""
        if not row:
            return ""

        lines: list[str] = []
        name = str(row.get("name") or "").strip()
        role = str(row.get("role") or "").strip()
        organization = str(row.get("organization") or "").strip()
        aliases = self._summary_profile_list(row.get("aliases_json"))[:12]
        email_addresses = self._summary_profile_list(row.get("email_addresses_json"))[:12]
        about = self._clip_summary_context(str(row.get("about") or ""), 320)
        if name:
            lines.append(f"Mailbox owner name: {name}")
        if aliases:
            lines.append(f"Owner aliases/usernames: {', '.join(aliases)}")
        if email_addresses:
            lines.append(f"Owner email addresses: {', '.join(email_addresses)}")
        if role:
            lines.append(f"Role: {role}")
        if organization:
            lines.append(f"Organization: {organization}")
        if about:
            lines.append(f"About: {about}")
        return "\n".join(lines)


    @staticmethod
    def _summary_profile_list(value: object) -> list[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value or "[]")
            except json.JSONDecodeError:
                return []
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            text = " ".join(str(item or "").split()).strip()
            if text and text not in result:
                result.append(text)
        return result


    @staticmethod
    def _clip_summary_context(value: str, limit: int) -> str:
        text = " ".join(value.split()).strip()
        if len(text) <= limit:
            return text
        return text[: limit - 1].rstrip() + "..."


    @staticmethod
    def _interval_summary_source_batches(source_text: str) -> list[str]:
        lines = [line for line in source_text.splitlines() if line.strip()]
        if len(lines) <= INTERVAL_SUMMARY_INPUT_BATCH_SIZE:
            return [source_text]
        batches = []
        for index in range(0, len(lines), INTERVAL_SUMMARY_INPUT_BATCH_SIZE):
            batches.append("\n".join(lines[index : index + INTERVAL_SUMMARY_INPUT_BATCH_SIZE]))
        return batches


    @staticmethod
    def _dedupe_interval_summary_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, tuple[int, ...]]] = set()
        for entry in entries:
            summary = " ".join(str(entry.get("summary") or "").split()).strip()
            raw_ids = entry.get("message_ids")
            if not summary or not isinstance(raw_ids, list):
                continue
            message_ids = []
            for value in raw_ids:
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id > 0 and message_id not in message_ids:
                    message_ids.append(message_id)
            key = (summary.lower(), tuple(message_ids))
            if message_ids and key not in seen:
                result.append({"summary": summary, "message_ids": message_ids})
                seen.add(key)
        return result


    @staticmethod
    def _interval_entries_source_text(entries: list[dict[str, Any]]) -> str:
        lines = []
        for entry in entries:
            message_ids = entry.get("message_ids")
            if not isinstance(message_ids, list):
                continue
            ids = ", ".join(str(message_id) for message_id in message_ids)
            summary = " ".join(str(entry.get("summary") or "").split()).strip()
            if ids and summary:
                lines.append(f"- mail_ids=[{ids}]: {summary}")
        return "\n".join(lines)


    @staticmethod
    def _interval_merge_source_text(entries: list[dict[str, Any]]) -> tuple[str, dict[int, list[int]]]:
        """Render preliminary entries as fake mail rows for the final merge pass.

        The model sees the existing prompt shape, but the IDs are compact entry references.
        """
        lines = []
        id_map: dict[int, list[int]] = {}
        for index, entry in enumerate(entries, start=1):
            raw_ids = entry.get("message_ids")
            if not isinstance(raw_ids, list):
                continue
            message_ids = []
            for value in raw_ids:
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id > 0 and message_id not in message_ids:
                    message_ids.append(message_id)
            summary = " ".join(str(entry.get("summary") or "").split()).strip()
            if not message_ids or not summary:
                continue
            synthetic_id = len(id_map) + 1
            id_map[synthetic_id] = message_ids
            lines.append(f"- mail_id={synthetic_id}; {summary}")
        return "\n".join(lines), id_map


    @classmethod
    def _expand_interval_merge_entries(
        cls,
        summary_json: object,
        id_map: dict[int, list[int]],
    ) -> list[dict[str, Any]]:
        # Convert merge-output synthetic IDs back to real message IDs before storing.
        # Unknown IDs are ignored; if that leaves no valid entries, the caller keeps the
        # deduplicated pre-merge entries instead of losing the whole interval.
        expanded = []
        for entry in cls._interval_summary_entries(summary_json):
            message_ids = []
            for synthetic_id in entry["message_ids"]:
                for message_id in id_map.get(int(synthetic_id), []):
                    if message_id not in message_ids:
                        message_ids.append(message_id)
            if message_ids:
                expanded.append({"summary": entry["summary"], "message_ids": message_ids})
        return cls._dedupe_interval_summary_entries(expanded)


    @staticmethod
    def _interval_summary_entries(summary_json: object) -> list[dict[str, Any]]:
        if not isinstance(summary_json, dict):
            return []
        entries = summary_json.get("entries")
        if not isinstance(entries, list):
            return []
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            summary = " ".join(str(entry.get("summary") or "").split()).strip()
            raw_ids = entry.get("message_ids")
            if not summary or not isinstance(raw_ids, list):
                continue
            message_ids = []
            for value in raw_ids:
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id > 0 and message_id not in message_ids:
                    message_ids.append(message_id)
            if message_ids:
                normalized.append({"summary": summary, "message_ids": message_ids})
        return normalized
