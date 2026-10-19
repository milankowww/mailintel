"""Interval summaries: one provider call per durable continuation."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from mailintel.services.llm import LLMContentValidationError, LLMInputError


class IntervalQueue:
    def __init__(self, pipeline: Any):
        self.pipeline = pipeline

    def register(self, runtime: Any) -> None:
        runtime.register("interval_daily", self.handle, capability="daily_summary")
        runtime.register("interval_rollup", self.handle, capability="rollup_summary")

    def discover(self, repository: Any) -> None:
        timezone = self.pipeline.settings.summary_timezone or "UTC"
        rows = repository.list_requested_interval_summaries(limit=25)
        rows += repository.list_stale_daily_interval_summaries(timezone, limit=25)
        rows += repository.list_stale_rollup_interval_summaries(limit=25)
        canonical = str(repository.get_pipeline_settings().get("canonical_embedding_model") or "")
        if canonical:
            from mailintel.persistence.repositories.legacy import interval_summary_entries_text_sql
            entries_text = interval_summary_entries_text_sql("s")
            pending = repository._execute(
                f"""SELECT s.id FROM mailbox_interval_summaries s
                WHERE s.refreshed_at IS NOT NULL AND {entries_text} <> ''
                  AND (s.embedding_status IN ('pending', 'failed', 'running')
                    OR s.embedding_needs_refresh OR s.embedding_model IS DISTINCT FROM %s)
                ORDER BY s.updated_at, s.id LIMIT 25""", (canonical,)
            ).fetchall()
            for item in pending:
                self._enqueue_embedding(repository, int(item["id"]), canonical)
        seen = set()
        for row in rows:
            if int(row["id"]) not in seen:
                seen.add(int(row["id"]))
                self.enqueue(repository, row, timezone)

    def _payload(self, repository: Any, row: dict, timezone: str) -> dict | None:
        daily = row["summary_type"] == "daily"
        if daily:
            inputs = repository.list_daily_summary_inputs(int(row["mailbox_id"]), row["interval_start_date"], timezone)
            lines = []
            for item in inputs:
                received = item.get("received_at")
                day = received.date().isoformat() if hasattr(received, "date") else str(row["interval_start_date"])
                sender = item.get("sender_name") or item.get("sender_address") or "unknown sender"
                lines.append(f"- mail_id={int(item['id'])}; {day}; {sender}; {item.get('subject') or '(no subject)'}: {item.get('summary_text') or item.get('preview_text') or ''}")
        else:
            inputs = repository.list_rollup_summary_inputs(int(row["mailbox_id"]), row["interval_start_date"], int(row["period_days"]))
            lines = []
            for item in inputs:
                for entry in self.pipeline._interval_summary_entries(item.get("summary_json")):
                    ids = ", ".join(str(value) for value in entry["message_ids"])
                    lines.append(f"- {item['interval_start_date']}; mail_ids=[{ids}]: {entry['summary']}")
        source = "\n".join(lines)
        if not source and not row.get("request_rebuild"):
            return
        context = self.pipeline._summary_user_context(repository)
        mailbox_name = str(row.get("mailbox_name") or row.get("provider_mailbox_id") or row["mailbox_id"])
        revision = hashlib.sha256(json.dumps([
            source, context, str(row.get("request_rebuild") or ""), timezone, mailbox_name,
            row["summary_type"], str(row["interval_start_date"]), int(row["period_days"]),
        ], ensure_ascii=False).encode()).hexdigest()
        payload = {
            "summary_id": int(row["id"]), "source_revision": revision,
            "summary_type": str(row["summary_type"]), "interval_start_date": str(row["interval_start_date"]),
            "period_days": int(row["period_days"]),
            "mailbox_name": str(row.get("mailbox_name") or row.get("provider_mailbox_id") or row["mailbox_id"]),
            "batches": self.pipeline._interval_summary_source_batches(source) if source else [],
            "index": 0, "entries": [], "phase": "batch", "user_context": context,
        }
        return payload

    def enqueue(self, repository: Any, row: dict, timezone: str) -> None:
        payload = self._payload(repository, row, timezone)
        if payload is None:
            return
        key = f"interval:{row['id']}"
        existing = repository._execute("SELECT payload FROM work_queue WHERE dedup_key = %s", (key,)).fetchone()
        if existing and existing["payload"].get("source_revision") == payload["source_revision"]:
            return
        daily = payload["summary_type"] == "daily"
        repository.queue_enqueue("interval_daily" if daily else "interval_rollup", key, payload, priority=3 if daily else 4)

    def _current(self, repository: Any, payload: dict) -> bool:
        row = repository.get_interval_summary_embedding_detail(int(payload["summary_id"]))
        if row is None:
            return False
        timezone = self.pipeline.settings.summary_timezone or "UTC"
        latest = self._payload(repository, row, timezone)
        if latest is None:
            return False
        if latest["source_revision"] == payload["source_revision"]:
            return True
        self.enqueue(repository, row, timezone)
        return False

    def handle(self, repository: Any, job: dict, preset: Any):
        payload = dict(job["payload"])
        batches = list(payload["batches"])
        entries = list(payload["entries"])
        merge = payload["phase"] == "merge"
        if not batches:
            return lambda: self._store(repository, payload, [])
        if merge:
            source, id_map = self.pipeline._interval_merge_source_text(entries)
            source = "These are preliminary evidence entries for the same interval. Merge related entries into a concise interval summary. Do not keep one entry per email when several entries describe the same topic.\n" + source
        else:
            index = int(payload["index"])
            source = batches[index]
            if len(batches) > 1:
                source = f"Input batch {index + 1} of {len(batches)} for the same interval. Return entries only for this batch.\n{source}"
        repository.rollback()
        try:
            result = self.pipeline.llm_service.summarize_interval(
                preset, summary_type=payload["summary_type"], interval_start_date=payload["interval_start_date"],
                period_days=payload["period_days"], mailbox_name=payload["mailbox_name"], source_text=source,
                user_context=payload["user_context"], priority=int(job["priority"]), client_id="interval",
            )
            generated = (self.pipeline._expand_interval_merge_entries(result, id_map) if merge else self.pipeline._interval_summary_entries(result))
            if not generated:
                raise LLMContentValidationError("Interval summary did not include valid evidence entries")
        except LLMInputError:
            if not merge:
                lines = batches[int(payload["index"])].splitlines()
                if len(lines) < 2:
                    raise
                middle = len(lines) // 2
                index = int(payload["index"])
                payload["batches"] = batches[:index] + ["\n".join(lines[:middle]), "\n".join(lines[middle:])] + batches[index + 1:]
                return lambda: self._continue(repository, job, payload)
            return self._merge_fallback(repository, payload, entries)
        except LLMContentValidationError:
            if not merge:
                raise
            return self._merge_fallback(repository, payload, entries)
        if merge:
            return lambda: self._store(repository, payload, generated)
        payload["entries"] = self.pipeline._dedupe_interval_summary_entries(entries + generated)
        payload["index"] = int(payload["index"]) + 1
        if payload["index"] < len(batches):
            return lambda: self._continue(repository, job, payload)
        elif len(batches) > 1:
            payload["phase"] = "merge"
            return lambda: self._continue(repository, job, payload)
        else:
            return lambda: self._store(repository, payload, payload["entries"])

    def _merge_fallback(self, repository: Any, payload: dict, entries: list):
        # Preserve existing evidence if a merge cannot fit or emits invalid JSON.
        from mailintel.services.pipeline import INTERVAL_SUMMARY_MERGE_FALLBACK_WARNING
        entries.append({"summary": INTERVAL_SUMMARY_MERGE_FALLBACK_WARNING, "message_ids": []})
        return lambda: self._store(repository, payload, entries, fallback=True)

    def _continue(self, repository: Any, job: dict, payload: dict) -> None:
        if not self._current(repository, payload):
            return
        repository.queue_enqueue(job["kind"], job["dedup_key"], payload, priority=int(job["priority"]))

    def _store(self, repository: Any, payload: dict, entries: list, fallback: bool = False) -> None:
        if not self._current(repository, payload):
            return
        summary_id = int(payload["summary_id"])
        if fallback:
            repository.mark_interval_summary_merge_fallback(summary_id, {"entries": entries})
        else:
            repository.mark_interval_summary_fresh(summary_id, {"entries": entries})
        canonical = str(repository.get_pipeline_settings().get("canonical_embedding_model") or "")
        if canonical:
            IntervalQueue._enqueue_embedding(repository, summary_id, canonical)

    @staticmethod
    def _enqueue_embedding(repository: Any, summary_id: int, model: str) -> None:
        repository.queue_enqueue("embed_interval", f"embed_interval:{summary_id}",
                                 {"summary_id": summary_id, "embedding_model": model}, priority=4)
