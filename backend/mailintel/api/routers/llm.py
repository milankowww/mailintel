from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...audit import audit_event
from ...auth import secured_route
from ...models import PipelineSettings
from ..dependencies import AppContext, llm_preset_from_payload, raise_name_conflict, route_payload
from ..schemas import LLMPresetPayload, PipelineSettingsPayload, PresetReorderPayload, QueuePriorityPayload
from ..serializers import row_to_dict


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository

    @secured_route(app, "GET", "/api/llm-presets", capability="mail.read")
    def list_llm_presets():
        return {"llm_presets": [{**row_to_dict(preset), "capabilities": [name for name in preset.get("capabilities_json") or [] if name != "chat"]} for preset in repository.list_llm_presets()]}

    @secured_route(app, "GET", "/api/pipeline-settings", capability="mail.read")
    def get_pipeline_settings():
        return {"pipeline_settings": {"canonical_embedding_model": repository.get_pipeline_settings().get("canonical_embedding_model", "")}}

    @secured_route(app, "POST", "/api/llm-presets", capability="settings.write", audit="llm_preset.saved")
    def save_llm_preset(payload: LLMPresetPayload):
        payload = route_payload(payload)
        try:
            preset_id, stale_count = repository.upsert_llm_preset(llm_preset_from_payload(payload))
        except Exception as exc:
            raise_name_conflict(exc, "llm preset")
        ctx.work_queue_runtime.refresh_presets()
        audit_event("llm_preset.saved", preset_id=preset_id, stale_embedding_count=stale_count)
        return {"status": "ok", "llm_preset_id": preset_id, "stale_embedding_count": stale_count}

    @secured_route(app, "POST", "/api/llm-presets/check", capability="settings.write")
    def check_llm_preset(payload: LLMPresetPayload):
        payload = route_payload(payload)
        preset_id = payload.get("id")
        preset = repository.get_llm_preset(int(preset_id)) if preset_id else None
        if preset is None or not preset.get("enabled"):
            raise HTTPException(status_code=400, detail="Save and enable the preset before checking it. The check uses its saved configuration.")
        if not any(str(preset.get(field) or "").strip() for field in ("text_model", "embedding_model")):
            raise HTTPException(status_code=400, detail="The preset needs at least one configured model for this check.")
        job = repository.queue_enqueue("preset_check", f"preset-check:{preset_id}",
                                       {"preset_id": int(preset_id)}, priority=1, preset_id=int(preset_id))
        return {"status": "accepted", "job_id": job["id"]}

    @secured_route(app, "POST", "/api/llm-presets/reorder", capability="settings.write")
    def reorder_llm_presets(payload: PresetReorderPayload):
        payload = route_payload(payload)
        items = payload.get("presets", [])
        if not isinstance(items, list):
            raise HTTPException(status_code=400, detail="presets must be a list")
        priorities: list[tuple[int, int]] = []
        for item in items:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="each preset priority must be an object")
            priorities.append((int(item["id"]), int(item["priority"])))
        repository.update_llm_preset_priorities(priorities)
        ctx.work_queue_runtime.refresh_presets()
        return {"status": "ok", "llm_presets": [{**row_to_dict(preset), "capabilities": [name for name in preset.get("capabilities_json") or [] if name != "chat"]} for preset in repository.list_llm_presets()]}

    @secured_route(app, "DELETE", "/api/llm-presets/{preset_id}", capability="settings.write", audit="llm_preset.deleted")
    def delete_llm_preset(preset_id: int):
        deleted = repository.delete_llm_preset(preset_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="llm preset not found")
        ctx.work_queue_runtime.refresh_presets()
        audit_event("llm_preset.deleted", preset_id=preset_id)
        return {"status": "ok", "deleted_llm_preset_id": preset_id}

    @secured_route(app, "POST", "/api/pipeline-settings", capability="settings.write")
    def save_pipeline_settings(payload: PipelineSettingsPayload):
        payload = (
            payload.model_dump(exclude_unset=True)
            if hasattr(payload, "model_dump")
            else route_payload(payload)
        )
        current = repository.get_pipeline_settings()
        canonical_embedding_model = str(
            payload.get("canonical_embedding_model", current.get("canonical_embedding_model") or "")
        )
        settings_payload = PipelineSettings(canonical_embedding_model=canonical_embedding_model)
        updated = repository.update_pipeline_settings(settings_payload)
        ctx.work_queue_runtime.refresh_presets()
        return {"pipeline_settings": {"canonical_embedding_model": updated.get("canonical_embedding_model", "")}}

    @secured_route(app, "GET", "/api/troubleshooting", capability="settings.write")
    def troubleshooting(job_id: int | None = None, worker_id: str | None = None,
                        preset_id: int | None = None, source_id: int | None = None,
                        action: str | None = None, limit: int = 200):
        filters = {name: value for name, value in {
            "job_id": job_id, "worker_id": worker_id, "preset_id": preset_id,
            "source_id": source_id, "action": action,
        }.items() if value is not None and value != ""}
        limit = min(max(limit, 1), 5000)
        snapshot = repository.queue_snapshot(limit=limit, filters=filters)
        snapshot["events"] = repository.queue_events(filters=filters, limit=limit)
        return {key: [row_to_dict(row) for row in rows] for key, rows in snapshot.items()}

    @secured_route(app, "GET", "/api/work-queue", capability="settings.write")
    def list_queue(limit: int = 100, offset: int = 0, state: str | None = None,
                   job_id: int | None = None, worker_id: str | None = None,
                   preset_id: int | None = None, source_id: int | None = None,
                   q: str | None = None, kind: str | None = None):
        if state not in (None, "new", "processing", "paused"):
            raise HTTPException(status_code=400, detail="state must be new, processing or paused")
        filters = {name: value for name, value in {
            "job_id": job_id, "worker_id": worker_id, "preset_id": preset_id,
            "source_id": source_id,
        }.items() if value is not None and value != ""}
        page = repository.queue_list(limit=min(max(limit, 1), 500), offset=max(offset, 0), state=state, filters=filters,
                                     q=(q or "").strip()[:200], kind=(kind or "").strip() or None)
        return {**page, "jobs": [row_to_dict(row) for row in page["jobs"]]}

    def queue_result(row):
        if row is None:
            raise HTTPException(status_code=404, detail="queue job not found")
        return {"status": "ok", "job": row_to_dict({key: value for key, value in row.items() if key != "payload"})}

    @secured_route(app, "PATCH", "/api/work-queue/{job_id}/priority", capability="settings.write")
    def reprioritize_queue_job(job_id: int, payload: QueuePriorityPayload):
        return queue_result(repository.queue_update_priority(job_id, payload.priority))

    @secured_route(app, "POST", "/api/work-queue/{job_id}/pause", capability="settings.write")
    def pause_queue_job(job_id: int):
        with repository.transaction():
            row = repository.queue_pause(job_id)
            result = queue_result(row)
            ctx.llm_service.cancel(f"job:{job_id}")
        return result

    @secured_route(app, "POST", "/api/work-queue/{job_id}/resume", capability="settings.write")
    def resume_queue_job(job_id: int):
        return queue_result(repository.queue_resume(job_id))

    @secured_route(app, "DELETE", "/api/work-queue/{job_id}", capability="settings.write")
    def delete_queue_job(job_id: int):
        with repository.transaction():
            row = repository.queue_delete(job_id)
            result = queue_result(row)
            search_id = row.get("payload", {}).get("search_id")
            if search_id is not None:
                # A removed fanout job otherwise leaves its search waiting forever.
                repository.cancel_smart_search_job(int(search_id))
                repository.queue_cancel_prefix(f"search:{search_id}:", reason="Search task deleted")
                repository._execute("UPDATE smart_search_jobs SET work_json = '{}'::jsonb WHERE job_id = %s AND status = 'cancelled'", (int(search_id),))
            ctx.llm_service.cancel(f"job:{job_id}")
            if search_id is not None:
                ctx.llm_service.cancel(f"search:{search_id}")
        return result
