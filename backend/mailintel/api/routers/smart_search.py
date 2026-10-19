from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...audit import audit_event
from ...auth import secured_route
from ...services.smart_search import SmartSearchFailure
from ..dependencies import AppContext
from ..schemas import SmartSearchPayload


def _public_job(row: dict) -> dict:
    return {
        "search_id": int(row["job_id"]),
        "status": str(row["status"]),
        "phase": str(row.get("phase") or ""),
        "failed_stage": row.get("failed_stage"),
        "error_code": row.get("error_code"),
        "error": str(row.get("safe_error_text") or ""),
        "retryable": bool(row.get("retryable")),
        "cancel_requested": bool(row.get("cancel_requested")),
        "progress": {
            "completed": int(row.get("processed_count") or 0),
            "total": int(row.get("total_count") or 0),
        },
        "stage_trace": row.get("stage_trace_json") or {},
        "coverage": row.get("coverage_json") or {},
        "result": row.get("result_json") or {},
        "matching_sources": row.get("matching_sources_json") or [],
        "can_retry_summary": row.get("status") == "failed" and "rows" in (row.get("work_json") or {}),
        "route": {
            "planner_preset_id": row.get("planner_preset_id"),
            "embedding_preset_id": row.get("embedding_preset_id"),
            "compressor_preset_id": row.get("compressor_preset_id"),
            "answer_preset_id": row.get("answer_preset_id"),
            "embedding_model": str(row.get("embedding_model") or ""),
            "owner_timezone": str(row.get("owner_timezone") or "UTC"),
        },
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "completed_at": row.get("completed_at"),
    }


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository
    runtime = ctx.smart_search_runtime

    @secured_route(
        app,
        "POST",
        "/api/search",
        capability="mail.read",
        audit="smart_search.started",
        status_code=202,
    )
    def create_search(payload: SmartSearchPayload):
        try:
            search_id = runtime.submit(
                repository,
                question=payload.query,
                idempotency_key=payload.idempotency_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except SmartSearchFailure as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "stage": exc.stage,
                    "code": exc.code,
                    "message": exc.safe_message,
                    "retryable": exc.retryable,
                },
            ) from exc
        audit_event("smart_search.started", search_id=search_id)
        return {"search_id": search_id, "status": "queued"}

    @secured_route(app, "GET", "/api/search", capability="mail.read")
    def list_searches(limit: int = 50, before_id: int | None = None):
        if limit < 1 or limit > 100 or (before_id is not None and before_id < 1):
            raise HTTPException(status_code=400, detail="invalid search history page")
        rows = repository.list_smart_search_jobs(limit=limit + 1, before_id=before_id)
        page = rows[:limit]
        return {
            "searches": [dict(row) for row in page],
            "next_before_id": int(page[-1]["job_id"]) if len(rows) > limit and page else None,
        }

    @secured_route(app, "GET", "/api/search/{search_id}", capability="mail.read")
    def get_search(search_id: int):
        row = repository.get_smart_search_job(search_id)
        if row is None:
            raise HTTPException(status_code=404, detail="search not found")
        return {"search": _public_job(dict(row))}

    @secured_route(
        app,
        "DELETE",
        "/api/search/{search_id}",
        capability="mail.read",
        audit="smart_search.removed",
    )
    def delete_search(search_id: int):
        row = repository.get_smart_search_job(search_id)
        if row is None:
            raise HTTPException(status_code=404, detail="search not found")
        if not repository.delete_smart_search_job(search_id):
            raise HTTPException(status_code=409, detail="stop the search before removing it")
        audit_event("smart_search.removed", search_id=search_id)
        return {"removed": True}

    @secured_route(app, "POST", "/api/search/{search_id}/retry-summary", capability="mail.read",
                   audit="smart_search.summary_retried")
    def retry_summary(search_id: int):
        if repository.get_smart_search_job(search_id) is None:
            raise HTTPException(status_code=404, detail="search not found")
        try:
            runtime.retry_summary(repository, search_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_event("smart_search.summary_retried", search_id=search_id)
        return {"search": _public_job(dict(repository.get_smart_search_job(search_id)))}

    @secured_route(
        app,
        "POST",
        "/api/search/{search_id}/cancel",
        capability="mail.read",
        audit="smart_search.cancelled",
    )
    def cancel_search(search_id: int):
        with repository.transaction():
            repository._queue_lock()
            row = repository.cancel_smart_search_job(search_id)
            repository.queue_cancel_prefix(f"search:{search_id}:", reason="Search cancelled")
            repository._execute("UPDATE smart_search_jobs SET work_json = '{}'::jsonb WHERE job_id = %s AND status = 'cancelled'", (search_id,))
        if row is None:
            raise HTTPException(status_code=404, detail="search not found")
        runtime.llm_service.cancel(f"search:{search_id}")
        audit_event("smart_search.cancelled", search_id=search_id)
        return {"search": _public_job(dict(row))}
