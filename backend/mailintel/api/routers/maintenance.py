from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException

from ...audit import audit_event
from ...auth import secured_route
from ...config.backup import export_config, import_config
from ...connectors.base import ConnectorError
from ..dependencies import AppContext, connector_http_exception, required_text, route_payload
from ..schemas import ConfigBackupPayload, MaintenanceResetPayload


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository
    pipeline_runtime = ctx.pipeline_runtime
    password_encryptor = ctx.password_encryptor

    @secured_route(app, "GET", "/api/maintenance", capability="mail.read")
    def maintenance_status():
        return {
            "sources": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "provider_type": row["provider_type"],
                    "status": row["status"],
                }
                for row in repository.list_mail_accounts()
            ],
        }

    @secured_route(app, "POST", "/api/maintenance/reset-processed", capability="maintenance.destroy", audit="maintenance.reset_processed")
    def reset_processed_emails(payload: MaintenanceResetPayload):
        payload = route_payload(payload)
        raw_source_ids = payload.get("source_ids", [])
        if not isinstance(raw_source_ids, list):
            raise HTTPException(status_code=400, detail="source_ids must be a list")
        try:
            source_ids = [int(item) for item in raw_source_ids]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="source_ids must contain integers") from exc
        if not source_ids:
            raise HTTPException(status_code=400, detail="select at least one source")

        accounts_by_id = {int(row["id"]): row for row in repository.list_mail_accounts()}
        unknown = [source_id for source_id in source_ids if source_id not in accounts_by_id]
        if unknown:
            raise HTTPException(status_code=404, detail=f"unknown source ids: {', '.join(str(item) for item in unknown)}")

        reset_result = repository.reset_processed_messages_for_accounts(source_ids)
        audit_event("maintenance.reset_processed", account_ids=source_ids, **reset_result)
        sync_results = []
        for source_id in source_ids:
            account = accounts_by_id[source_id]
            if str(account.get("status", "active")) != "active":
                sync_results.append({"source_id": source_id, "status": "error", "detail": "account is disabled; re-enable it before syncing"})
                continue
            try:
                result = pipeline_runtime.schedule_account_sync(source_id)
                sync_results.append({"source_id": source_id, "status": "ok", **result})
            except (ConnectorError, KeyError, TypeError, ValueError) as exc:
                sync_results.append({"source_id": source_id, "status": "error", "detail": str(connector_http_exception(exc).detail)})

        return {"status": "ok", **reset_result, "sync_results": sync_results}

    @secured_route(app, "POST", "/api/maintenance/remove-customization", capability="maintenance.destroy", audit="maintenance.remove_customization")
    def remove_customization_settings():
        result = repository.delete_customization_settings()
        audit_event("maintenance.remove_customization", **result)
        return {"status": "ok", **result}

    @secured_route(app, "POST", "/api/maintenance/remove-ai-settings", capability="maintenance.destroy", audit="maintenance.remove_ai_settings")
    def remove_ai_settings():
        deleted = repository.delete_all_llm_presets()
        ctx.work_queue_runtime.refresh_presets()
        audit_event("maintenance.remove_ai_settings", deleted_llm_presets=deleted)
        return {"status": "ok", "deleted_llm_presets": deleted}

    @secured_route(app, "POST", "/api/maintenance/reset-summaries", capability="maintenance.destroy", audit="maintenance.reset_summaries")
    def reset_interval_summaries():
        deleted = repository.delete_interval_summaries()
        audit_event("maintenance.reset_summaries", deleted_summaries=deleted)
        return {"status": "ok", "deleted_summaries": deleted}

    @secured_route(app, "POST", "/api/maintenance/rebuild-topics", capability="maintenance.destroy")
    def maintenance_rebuild_topics():
        pipeline_settings = repository.get_pipeline_settings()
        embedding_model = str(pipeline_settings.get("canonical_embedding_model") or "")
        if not embedding_model:
            raise HTTPException(status_code=400, detail="No canonical embedding model configured")
        return {"status": "ok", "rebuild": pipeline_runtime.request_cluster_refresh(embedding_model)}

    @secured_route(app, "POST", "/api/config/export", capability="admin.config", audit="config.exported")
    def export_portable_config(payload: ConfigBackupPayload):
        payload = route_payload(payload)
        path = Path(required_text(payload, "path"))
        passphrase = required_text(payload, "passphrase")
        result = export_config(repository, password_encryptor, path, passphrase)
        audit_event("config.exported", path=str(path), **result)
        return {"status": "ok", **result}

    @secured_route(app, "POST", "/api/config/import", capability="admin.config", audit="config.imported")
    def import_portable_config(payload: ConfigBackupPayload):
        payload = route_payload(payload)
        path = Path(required_text(payload, "path"))
        passphrase = required_text(payload, "passphrase")
        result = import_config(repository, password_encryptor, path, passphrase)
        ctx.work_queue_runtime.refresh_presets()
        audit_event("config.imported", path=str(path), **result)
        return {"status": "ok", **result}
