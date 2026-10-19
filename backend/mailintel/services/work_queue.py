"""Preset slots and separate deterministic worker lanes consume SQL work."""
from __future__ import annotations

import threading
import re
import asyncio
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Callable

from ..db import connect_database
from ..repositories import Repository
from .llm import LLMProviderError, LLMInputError, LLMContentValidationError, llm_preset_from_row


@dataclass(frozen=True)
class Handler:
    run: Callable
    operation: str
    model: str
    capability: str


class WorkQueueRuntime:
    def __init__(self, settings, llm_service):
        self.settings = settings
        self.llm_service = llm_service
        self.handlers = {}
        self.producers = []
        self._stop = threading.Event()
        self._threads = {}
        self._condition = threading.Condition()
        self._wanted_slots = set()
        self._running = False
        self.register('preset_check', self._check_preset, model='text')

    def register(self, kind, handler, *, operation='generation', model='text', capability=None):
        self.handlers[kind] = Handler(handler, operation, model, capability or kind)

    def register_producer(self, producer):
        self.producers.append(producer)

    def _repository(self):
        return Repository(connect_database(self.settings.database_url))

    def start(self):
        self._stop.clear()
        repository = self._repository()
        try:
            repository.queue_recover_processing()
            with self._condition:
                self._running = True
            self._reconcile(repository)
        finally:
            repository.connection.close()
        with self._condition:
            targets = [('discovery', self._discovery_loop, ()),
                       ('local', self._deterministic_loop, ('local', 'local'))]
            targets.extend((f'clustering:{slot}', self._deterministic_loop,
                            (f'clustering:{slot}', 'clustering'))
                           for slot in range(1, self.settings.cluster_worker_count + 1))
            for key, target, args in targets:
                thread = threading.Thread(target=target, args=args, name=f'queue-{key}', daemon=True)
                self._threads[key] = thread
                thread.start()

    def stop(self):
        with self._condition:
            self._running = False
            self._stop.set()
            self._condition.notify_all()
            threads = list(self._threads.values())
        self.llm_service.close()
        for thread in threads:
            thread.join(timeout=2)

    def _wait(self, seconds, worker=None):
        with self._condition:
            if not self._stop.is_set() and (worker is None or worker in self._wanted_slots):
                self._condition.wait(timeout=seconds)

    def _wanted(self, worker):
        with self._condition:
            return not self._stop.is_set() and worker in self._wanted_slots

    def _presets(self, repository):
        return repository.list_stage_preset_health('preset')

    def refresh_presets(self):
        repository = self._repository()
        try:
            self._reconcile(repository)
            with self._condition:
                self._condition.notify_all()
        finally:
            repository.connection.close()

    def _reconcile(self, repository):
        # Serialize fresh reads with API reconciliation, so an older discovery read
        # cannot restore slots just removed by a configuration change.
        with self._condition:
            if not self._running:
                return
            rows = self._presets(repository)
            wanted = {f'preset:{row["id"]}:{slot + 1}'
                      for row in rows
                      for slot in range(max(1, min(20, int(row.get('concurrency') or 1))))}
            changed = wanted != self._wanted_slots
            self._wanted_slots = wanted
            for row in rows:
                for slot in range(max(1, min(20, int(row.get('concurrency') or 1)))):
                    key = f'preset:{row["id"]}:{slot + 1}'
                    if key not in self._threads or not self._threads[key].is_alive():
                        thread = threading.Thread(target=self._preset_loop, args=(int(row['id']), slot, key), name=key, daemon=True)
                        self._threads[key] = thread
                        thread.start()
            if changed:
                self._condition.notify_all()

    def _discovery_loop(self):
        repository = self._repository()
        repository.queue_set_worker('discovery','online')
        try:
            while not self._stop.is_set():
                try:
                    self._reconcile(repository)
                    for producer in self.producers:
                        producer(repository)
                    repository.rollback()
                    repository.queue_set_worker('discovery','online')
                except Exception as exc:
                    repository.rollback()
                    repository.queue_set_worker('discovery','unavailable',reason=self._reason(exc))
                self._wait(self.settings.worker_poll_seconds)
        finally:
            repository.queue_set_worker('discovery','stopped')
            repository.connection.close()

    def _deterministic_loop(self, worker, operation):
        repository = self._repository()
        repository.queue_set_worker(worker, 'online')
        try:
            while not self._stop.is_set():
                try:
                    kinds = [kind for kind, handler in self.handlers.items() if handler.operation == operation]
                    job = repository.queue_claim(worker, None, kinds) if kinds else None
                    if job:
                        self._run(repository, job, None)
                        continue
                except Exception as exc:
                    repository.connection.rollback()
                    # A deterministic worker error does not take a healthy preset offline.
                    repository.queue_set_worker(worker, 'unavailable', reason=self._reason(exc))
                self._wait(self.settings.worker_poll_seconds)
                repository.queue_set_worker(worker, 'online')
        finally:
            repository.queue_set_worker(worker, 'stopped')
            repository.connection.close()

    def _compatible(self, row, handler, canonical):
        config = llm_preset_from_row(row)
        if handler.capability == 'preset_check':
            return self.llm_service.is_enabled(config)
        if not self.llm_service.is_enabled(config) or handler.capability not in config.capabilities:
            return False
        if not getattr(config, f'{handler.model}_model', ''):
            return False
        return handler.operation != 'embedding' or not canonical or config.embedding_model == canonical

    def _allowed(self, row, rows, canonical):
        return [kind for kind, handler in self.handlers.items()
                if handler.operation not in ('local', 'clustering') and self._compatible(row, handler, canonical)
                and (kind == 'preset_check' or not any(int(other['priority']) < int(row['priority'])
                            and other['health_status'] == 'healthy'
                            and self._compatible(other, handler, canonical) for other in rows))]

    def _preset_loop(self, preset_id, slot, worker):
        repository = self._repository()
        repository.queue_set_worker(worker, 'online', preset_id=preset_id)
        try:
            while self._wanted(worker):
                rows = self._presets(repository)
                row = next((item for item in rows if int(item['id']) == preset_id), None)
                if row is None or slot >= int(row.get('concurrency') or 1):
                    break
                if self.llm_service.pause_controller.is_paused():
                    self._wait(self.settings.worker_poll_seconds, worker)
                    continue
                if row['health_status'] != 'healthy':
                    repository.queue_set_worker(worker, 'unavailable', preset_id=preset_id, reason=row.get('last_error_text') or '')
                    # Slot one performs recovery, occupying its ordinary preset slot.
                    if slot == 0 and self._wanted(worker) and any(int(item['id']) == preset_id for item in repository.list_due_preset_cooldown_recoveries('preset')):
                        self._recover(repository, row, worker)
                    self._wait(self.settings.preset_cooldown_recovery_interval_seconds, worker)
                    continue
                repository.queue_set_worker(worker, 'online', preset_id=preset_id)
                with repository.transaction():
                    repository._queue_lock()
                    rows = self._presets(repository)
                    row = next((item for item in rows if int(item['id']) == preset_id), None)
                    canonical = str(repository.get_pipeline_settings().get('canonical_embedding_model') or '')
                    kinds = self._allowed(row, rows, canonical) if row and row['health_status'] == 'healthy' and slot < int(row.get('concurrency') or 1) and not self.llm_service.pause_controller.is_paused() else []
                    job = repository.queue_claim(worker, preset_id, kinds, embedding_model=canonical or None) if kinds else None
                if job:
                    self._run(repository, job, llm_preset_from_row(row))
                else:
                    self._wait(self.settings.worker_poll_seconds, worker)
        finally:
            repository.queue_set_worker(worker, 'stopped', preset_id=preset_id if repository.get_llm_preset(preset_id) else None)
            repository.connection.close()
            with self._condition:
                if self._threads.get(worker) is threading.current_thread():
                    self._threads.pop(worker)
                    # Re-enabling while this slot was retiring must not wait for
                    # discovery. The old slot has finished its accepted work.
                    if self._running and worker in self._wanted_slots:
                        thread = threading.Thread(target=self._preset_loop, args=(preset_id, slot, worker), name=worker, daemon=True)
                        self._threads[worker] = thread
                        thread.start()

    @staticmethod
    def _provider_failure(exc):
        while exc is not None:
            if isinstance(exc, (LLMInputError, LLMContentValidationError)):
                return False
            if isinstance(exc, LLMProviderError):
                return True
            exc = exc.__cause__ or exc.__context__
        return False

    @staticmethod
    def _reason(exc):
        # Provider error bodies and validation samples may echo private mail content.
        current = exc
        while current is not None:
            if isinstance(current, LLMInputError):
                return "LLM input rejected"
            if isinstance(current, LLMContentValidationError):
                return "LLM structured output invalid"
            if isinstance(current, LLMProviderError):
                text = str(current)
                status = re.search(r'LLM request failed: (\d{3})', text)
                if status:
                    return f'Provider HTTP {status.group(1)}'
                if 'timed out' in text.lower():
                    return 'Provider request timed out'
                for marker in ('connection refused', 'name or service not known', 'certificate verify failed'):
                    if marker in text.lower():
                        return f'Provider {marker}'
                return 'Provider request failed'
            current = current.__cause__ or current.__context__
        return type(exc).__name__ + (f': {exc.code}' if hasattr(exc, 'code') else '')

    def _run(self, repository, job, preset):
        try:
            scope = getattr(self.llm_service, 'cancellation_scope', None)
            with scope(f"job:{job['id']}") if scope else nullcontext():
                if not repository.queue_is_owned(job):
                    return
                apply = self.handlers[job['kind']].run(repository, job, preset)
                repository.queue_complete(job, apply=apply)
        except asyncio.CancelledError:
            # Shutdown leaves ownership intact for normal startup recovery.
            repository.rollback()
        except Exception as exc:
            repository.connection.rollback()
            with repository.transaction():
                repository._queue_lock()
                if repository._queue_owned(job) is None:
                    return
                if preset is not None and repository.get_llm_preset(preset.id) and self._provider_failure(exc):
                    handler = self.handlers[job['kind']]
                    operation, model = self._check_resource(preset) if job['kind'] == 'preset_check' else (
                        handler.operation, getattr(preset, handler.model + '_model', ''))
                    self._fail(repository, preset.id, self._reason(exc), operation=operation, model=model)
                repository.queue_requeue(job, self._reason(exc))
            worker = job.get('worker_id')
            self._wait(self.settings.worker_poll_seconds, worker if worker and worker.startswith('preset:') else None)

    def _fail(self, repository, preset_id, reason, *, operation=None, model=None):
        if operation is not None:
            reason = f"[{operation}:{model}] {reason}"
        health = next((row for row in self._presets(repository) if int(row['id']) == preset_id), {})
        delays = (3, 10, 30, 60)
        repository.mark_preset_stage_failure(preset_id, 'preset', reason, delays[min(int(health.get('consecutive_failures') or 0), 3)])
        for slot in range(int(health.get('concurrency') or 1)):
            repository.queue_set_worker(f'preset:{preset_id}:{slot + 1}', 'unavailable', preset_id=preset_id, reason=reason)

    def _probe(self, preset, operation, model):
        if operation == 'embedding':
            self.llm_service.embed_texts(preset, texts=['health probe'], raise_on_error=True)
        else:
            self.llm_service.probe_generation(replace(preset, text_model=model))

    def _check_preset(self, repository, job, preset):
        # A check is a single call. Recovery checks the particular failed resource.
        operation, model = self._check_resource(preset)
        self._probe(preset, operation, model)
        return lambda: repository.mark_preset_stage_success(preset.id, 'preset')

    @staticmethod
    def _check_resource(preset):
        model = preset.text_model
        return ('generation', model) if model else ('embedding', preset.embedding_model)

    def _recover(self, repository, row, worker):
        preset = llm_preset_from_row(row)
        reason = str(row.get('last_error_text') or '')
        operation, model = 'generation', preset.text_model
        if reason.startswith('[') and '] ' in reason:
            marker = reason[1:reason.index('] ')]
            if ':' in marker:
                operation, model = marker.split(':', 1)
        elif not model:
            operation, model = 'embedding', preset.embedding_model
        if operation == 'embedding':
            model = preset.embedding_model
        elif model != preset.text_model:
            model = preset.text_model
        if not model:
            operation, model = self._check_resource(preset)
        try:
            scope = getattr(self.llm_service, 'cancellation_scope', None)
            with scope(f"recovery:{worker}") if scope else nullcontext():
                self._probe(preset, operation, model)
            if not repository.get_llm_preset(preset.id):
                return
            with repository.transaction():
                repository.mark_preset_stage_success(preset.id, 'preset')
                repository.queue_set_worker(worker, 'online', preset_id=preset.id)
        except asyncio.CancelledError:
            repository.rollback()
        except Exception as exc:
            if not repository.get_llm_preset(preset.id):
                return
            with repository.transaction():
                self._fail(repository, preset.id, self._reason(exc), operation=operation, model=model)
