from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException

from ...audit import audit_event
from ...auth import secured_route
from ...connectors.base import ConnectorError
from ...services.llm import llm_pause_controller, llm_preset_from_row
from ..dependencies import AppContext, connector_http_exception
from ..serializers import row_to_dict, search_activity_label


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository
    settings = ctx.settings
    pipeline_runtime = ctx.pipeline_runtime

    @secured_route(app, "GET", "/api/llm-pause", capability="mail.read")
    def get_llm_pause():
        return {"paused": llm_pause_controller.is_paused()}

    @secured_route(app, "POST", "/api/llm-pause/toggle", capability="settings.write")
    def toggle_llm_pause():
        with repository.transaction():
            repository._queue_lock()
            paused = llm_pause_controller.toggle()
            if paused:
                repository.queue_interrupt_presets('Interrupted by global AI pause')
                ctx.llm_service.cancel()
        return {"paused": paused}

    @secured_route(app, "GET", "/api/jobs", capability="mail.read")
    def list_jobs(limit: int = 20):
        stale_after = max(settings.job_stale_after_minutes, 1)
        repository.expire_stale_running_jobs(datetime.now(UTC) - timedelta(minutes=stale_after))
        return {"jobs": [row_to_dict(row) for row in repository.list_jobs(limit=limit)]}

    @secured_route(app, "GET", "/api/status", capability="mail.read")
    def get_status():
        pipeline_settings = row_to_dict(repository.get_pipeline_settings())
        embedding_model = str(pipeline_settings.get("canonical_embedding_model") or "")
        processing = repository.get_pipeline_status_counts(embedding_model)
        clustering = repository.get_cluster_pipeline_status(embedding_model) if embedding_model else {
            "ready_embeddings": 0,
            "clustered_messages": 0,
            "status": "idle",
        }
        runtime = ctx.work_queue_runtime
        snapshot = repository.queue_activity()
        jobs = snapshot["jobs"]
        rows = repository.list_stage_preset_health("preset")
        active_streams: dict[int, list[dict]] = {}
        stream_activity = getattr(ctx.llm_service, "stream_activity", None)
        for stream in stream_activity() if stream_activity else []:
            if stream.get("preset_id") is not None:
                active_streams.setdefault(int(stream["preset_id"]), []).append(stream)
        search_stages = {"search_plan": "planning", "search_embed_query": "embedding",
                         "search_compress": "compression", "search_answer": "answering"}
        active_search_stages = {}
        for job in jobs:
            stage = search_stages.get(job["kind"])
            if job["state"] == "processing" and stage:
                active_search_stages[stage] = active_search_stages.get(stage, 0) + 1
        model_indicators = []
        for row in rows:
            preset = llm_preset_from_row(row)
            if preset is None:
                continue
            healthy = row["health_status"] == "healthy"
            allowed = set(runtime._allowed(row, rows, embedding_model)) if healthy else set()
            for operation in ("generation", "embedding"):
                if operation == "embedding" and not str(preset.embedding_model or "").strip():
                    continue
                if operation == "generation" and not str(preset.text_model or "").strip():
                    continue
                compatible = {kind: handler for kind, handler in runtime.handlers.items()
                              if kind != "preset_check" and handler.operation == operation and runtime._compatible(row, handler, embedding_model)}
                if not compatible:
                    continue
                running = [job for job in jobs if job["state"] == "processing"
                           and job.get("preset_id") == preset.id and job["kind"] in compatible]
                search_count = sum(job["kind"] in search_stages for job in running)
                pipeline_count = len(running) - search_count
                queued_count = sum(int(group["count"]) for group in snapshot["pending"]
                                   if group["kind"] in allowed and group["kind"] in compatible
                                   and group.get("target_preset_id") in (None, preset.id)
                                   and (not group.get("embedding_model") or group["embedding_model"] == embedding_model))
                selected = bool(set(compatible) & allowed)
                health = str(row["health_status"])
                routing = "selected" if selected else ("failed_cooldown" if not healthy else "standby_unchecked")
                state = ("failed_cooldown" if not healthy else "active_search" if search_count else "active_pipeline" if pipeline_count
                         else "queued" if queued_count
                         else "healthy_idle" if selected else "standby_unchecked")
                slot_jobs = {int(job['worker_id'].rsplit(':', 1)[-1]): job
                             for job in running if job.get('worker_id')}
                concurrency = int(row.get('concurrency') or 1)
                slots = []
                for number in sorted(set(range(1, concurrency + 1)) | set(slot_jobs)):
                    job = slot_jobs.get(number)
                    slot_state = ('active_search' if job and job['kind'] in search_stages
                                  else 'active_pipeline' if job else 'failed_cooldown' if not healthy
                                  else 'healthy_idle')
                    slots.append({'number': number, 'state': slot_state,
                                  'retiring': number > concurrency})
                model_indicators.append({
                    "preset_id": preset.id, "preset_name": preset.name, "priority": preset.priority,
                    "provider": preset.provider, "endpoint_url": preset.endpoint_url, "operation": operation,
                    "state": state, "routing_status": routing, "health_status": health,
                    "active_search_count": search_count, "active_pipeline_count": pipeline_count,
                    "active_count": len(running), "queued_count": queued_count,
                    "slots": slots,
                    "model_names": sorted({getattr(preset, f"{handler.model}_model") for handler in compatible.values()}),
                    "cooldown_until": row.get("cooldown_until"),
                    "last_error_text": row.get("last_error_text") or "",
                    "streams": active_streams.get(preset.id, []) if operation == "generation" else [],
                })
        mailboxes = []
        for row in repository.list_pipeline_mailbox_statuses(embedding_model):
            mailbox = row_to_dict(row)
            total_messages = int(mailbox.get("total_messages") or 0)
            analyzed_done = int(mailbox.get("analyzed_done") or 0)
            embedded_done = int(mailbox.get("embedded_done") or 0)
            crawl_total = int(mailbox.get("crawl_total_count") or 0)
            crawl_processed = int(mailbox.get("crawl_processed_count") or 0)
            crawl_status = str(mailbox.get("crawl_job_status") or mailbox.get("sync_status") or "idle")
            crawl_done = min(crawl_processed, crawl_total) if crawl_status == "running" else 0
            crawl_total = crawl_total if crawl_status == "running" else 0
            mailbox["crawl_done"] = max(crawl_done, 0)
            mailbox["crawl_total"] = max(crawl_total, 0)
            mailbox["analysis_done"] = analyzed_done
            mailbox["analysis_total"] = total_messages
            mailbox["embedding_done"] = embedded_done
            mailbox["embedding_total"] = total_messages
            mailbox["crawl_status"] = crawl_status
            mailbox["ingesting_done"] = max(analyzed_done, embedded_done)
            mailbox["ingesting_total"] = total_messages
            mailboxes.append(mailbox)
        summaries = {**processing['summaries'], 'in_progress': processing['summaries']['running'],
                     'status': 'running' if processing['summaries']['running'] else 'queued' if processing['summaries']['queued'] else 'idle'}
        clustering_done = int(clustering["clustered_messages"] or 0)
        clustering_description = ""
        return {
            "status": "ok",
            "embedding_model": embedding_model,
            "pipeline_settings": pipeline_settings,
            "analyses": {
                "done": processing["analyses"]["done"],
                "total": processing["analyses"]["total"],
                "in_progress": processing["analyses"]["running"],
                "queued": processing["analyses"]["queued"],
                "retrying": processing["analyses"]["retrying"],
            },
            "embeddings": {
                "done": processing["embeddings"]["done"],
                "total": processing["embeddings"]["total"],
                "in_progress": processing["embeddings"]["running"],
                "queued": processing["embeddings"]["queued"],
                "retrying": processing["embeddings"]["retrying"],
                "claimable": processing["embeddings"].get("claimable", 0),
                "waiting_for_analysis": processing["embeddings"].get("waiting_for_analysis", 0),
            },
            "message_embeddings": {
                "done": processing["message_embeddings"]["done"],
                "total": processing["message_embeddings"]["total"],
                "in_progress": processing["message_embeddings"]["running"],
                "queued": processing["message_embeddings"]["queued"],
                "retrying": processing["message_embeddings"]["retrying"],
                "claimable": processing["message_embeddings"].get("claimable", 0),
                "waiting_for_analysis": processing["message_embeddings"].get("waiting_for_analysis", 0),
            },
            "interval_summary_embeddings": {
                "done": processing["interval_summary_embeddings"]["done"],
                "total": processing["interval_summary_embeddings"]["total"],
                "in_progress": processing["interval_summary_embeddings"]["running"],
                "queued": processing["interval_summary_embeddings"]["queued"],
                "retrying": processing["interval_summary_embeddings"]["retrying"],
                "claimable": processing["interval_summary_embeddings"].get("claimable", 0),
                "waiting_for_analysis": processing["interval_summary_embeddings"].get("waiting_for_analysis", 0),
            },
            "clustering": {
                "done": clustering_done,
                "total": clustering["ready_embeddings"],
                "in_progress": clustering.get("running", 0),
                "queued": clustering.get("queued", 0),
                "retrying": clustering.get("retrying", 0),
                "status": clustering.get("status", "idle"),
                "description": clustering_description,
            },
            "summaries": summaries,
            "search_activity": {
                "active": bool(active_search_stages),
                "stages": active_search_stages,
                "label": search_activity_label(active_search_stages),
            },
            "models": model_indicators,
            "mailboxes": mailboxes,
            "updated_at": datetime.now(UTC).isoformat(),
        }

    @secured_route(app, "POST", "/api/rebuild-embeddings", capability="sync.run")
    def rebuild_embeddings(payload: dict[str, object] | None = None):
        del payload
        return pipeline_runtime.request_embedding_refresh()

    @secured_route(app, "POST", "/api/rebuild-interest-matches", capability="sync.run")
    def rebuild_interest_matches(payload: dict[str, object] | None = None):
        del payload
        result = pipeline_runtime.request_analysis_refresh()
        return {"rebuilt_messages": result.get("marked_messages", 0), **result}

    @secured_route(app, "POST", "/api/sync/{account_id}", capability="sync.run", audit="sync.scheduled")
    def sync_account(account_id: int):
        account = repository.get_mail_account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        if str(account.get("status", "active")) != "active":
            raise HTTPException(status_code=400, detail="account is disabled; re-enable it before syncing")
        try:
            audit_event("sync.scheduled", account_id=account_id)
            return pipeline_runtime.schedule_account_sync(account_id)
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise connector_http_exception(exc) from exc
