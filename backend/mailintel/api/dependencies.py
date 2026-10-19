from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import FileResponse

from ..account_secrets import account_for_connector
from ..connectors.base import ConnectorError
from ..connectors.registry import ConnectorRegistry
from ..models import ALL_PRESET_CAPABILITIES, CAPABILITY_MODEL_FIELDS, LLMPreset
from ..repositories import Repository
from ..secrets import PasswordEncryptor
from ..services.chat import ChatService
from ..services.dashboard import DashboardService
from ..services.llm import LLMService
from ..services.pipeline import PipelineRuntime
from ..services.smart_search import SmartSearchRuntime
from ..services.work_queue import WorkQueueRuntime
from ..settings import AppSettings


@dataclass(frozen=True)
class AppContext:
    settings: AppSettings
    repository: Repository
    password_encryptor: PasswordEncryptor
    connector_registry: ConnectorRegistry
    dashboard_service: DashboardService
    chat_service: ChatService
    llm_service: LLMService
    pipeline_runtime: PipelineRuntime
    smart_search_runtime: SmartSearchRuntime
    work_queue_runtime: WorkQueueRuntime


def connector_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, ConnectorError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, KeyError):
        missing = str(exc.args[0])
        return HTTPException(
            status_code=400,
            detail=f"Account configuration is missing '{missing}'. For IMAP, fill in host, port, username, and password.",
        )
    return HTTPException(status_code=400, detail=str(exc))


def create_connector(ctx: AppContext, account: object):
    try:
        prepared_account = account_for_connector(dict(account), ctx.password_encryptor)
        return ctx.connector_registry.create(
            prepared_account["provider_type"],
            prepared_account["settings_json"],
            prepared_account["secret_locator"],
        )
    except (ConnectorError, KeyError, TypeError, ValueError) as exc:
        raise connector_http_exception(exc) from exc


def discover_mailboxes_for_account(ctx: AppContext, account: object):
    connector = create_connector(ctx, account)
    existing_by_provider_id = {
        row["provider_mailbox_id"]: bool(row["monitored"])
        for row in ctx.repository.list_mailboxes(account_id=int(account["id"]))
    }
    try:
        for mailbox in connector.list_mailboxes():
            mailbox.account_id = int(account["id"])
            if mailbox.provider_mailbox_id in existing_by_provider_id:
                mailbox.monitored = existing_by_provider_id[mailbox.provider_mailbox_id]
            ctx.repository.upsert_mailbox(mailbox)
    except (ConnectorError, KeyError, TypeError, ValueError) as exc:
        raise connector_http_exception(exc) from exc
    return ctx.repository.list_mailboxes(account_id=int(account["id"]))


def optional_int(payload: dict[str, object], field: str) -> int | None:
    value = payload.get(field)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be an integer") from exc


def number(payload: dict[str, object], field: str, default: float) -> float:
    try:
        return float(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be a number") from exc


def integer(payload: dict[str, object], field: str, default: int) -> int:
    try:
        return int(payload.get(field, default))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be an integer") from exc


def positive_integer(payload: dict[str, object], field: str, default: int) -> int:
    value = integer(payload, field, default)
    if value < 1:
        raise HTTPException(status_code=400, detail=f"{field} must be at least 1")
    return value


def string_list(payload: dict[str, object], field: str) -> list[str]:
    value = payload.get(field, [])
    if not isinstance(value, list):
        raise HTTPException(status_code=400, detail=f"{field} must be a list")
    return [str(item) for item in value]


def summary_period_days(start_date: date, duration: str) -> int:
    normalized = duration.strip().lower()
    if normalized == "day":
        return 1
    if normalized == "week":
        return 7
    if normalized == "month":
        if start_date.day != 1:
            raise HTTPException(status_code=400, detail="month summaries must start on the first day of the month")
        next_month = date(
            start_date.year + (1 if start_date.month == 12 else 0),
            1 if start_date.month == 12 else start_date.month + 1,
            1,
        )
        return (next_month - start_date).days
    raise HTTPException(status_code=400, detail="duration must be day, week, or month")


def object_map(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload.get(field, {})
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"{field} must be an object")
    return dict(value)


def required_text(payload: dict[str, object], field: str) -> str:
    value = str(payload.get(field, "")).strip()
    if not value:
        raise HTTPException(status_code=400, detail=f"{field} is required")
    return value


def optional_date_param(value: str | None, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{field} must be YYYY-MM-DD") from exc


def date_range_params(from_date: str | None, to_date: str | None) -> tuple[date | None, date | None]:
    parsed_from = optional_date_param(from_date, "from_date")
    parsed_to = optional_date_param(to_date, "to_date")
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(status_code=400, detail="from_date must be on or before to_date")
    return parsed_from, parsed_to


def raise_name_conflict(exc: Exception, entity: str) -> None:
    if exc.__class__.__name__ == "UniqueViolation":
        raise HTTPException(status_code=409, detail=f"{entity} name already exists") from exc
    raise exc


def llm_preset_from_payload(payload: dict[str, object], *, enabled: bool | None = None) -> LLMPreset:
    embedding_model = str(payload.get("embedding_model", "nomic-embed-text"))
    capabilities = payload.get("capabilities")
    if capabilities is None:
        capabilities = [name for name in ALL_PRESET_CAPABILITIES if str(payload.get(CAPABILITY_MODEL_FIELDS[name], "")).strip()]
    if isinstance(capabilities, list):
        capabilities = [name for name in capabilities if name != "chat"]
    if not isinstance(capabilities, list) or any(name not in CAPABILITY_MODEL_FIELDS for name in capabilities):
        raise HTTPException(status_code=400, detail="unknown preset capability")
    concurrency = integer(payload, "concurrency", 1)
    if not 1 <= concurrency <= 20:
        raise HTTPException(status_code=400, detail="preset concurrency must be between 1 and 20")
    return LLMPreset(
        concurrency=concurrency,
        capabilities=list(dict.fromkeys(capabilities)),
        id=optional_int(payload, "id"),
        name=str(payload.get("name", "Local Ollama")),
        provider=str(payload.get("provider", "ollama")),
        enabled=bool(payload.get("enabled", False)) if enabled is None else enabled,
        priority=integer(payload, "priority", 100),
        auth_mode=str(payload.get("auth_mode", "env")),
        secret_locator=str(payload.get("secret_locator", "")),
        endpoint_url=str(payload.get("endpoint_url", "http://127.0.0.1:11434")),
        text_model=str(payload.get("text_model", "llama3.1")),
        embedding_model=embedding_model,
        temperature=number(payload, "temperature", 0.2),
        text_context_window=positive_integer(payload, "text_context_window", 8192),
        embedding_context_window=positive_integer(payload, "embedding_context_window", 8192),
        max_output_tokens=integer(payload, "max_output_tokens", 800),
        initial_timeout_seconds=payload.get("initial_timeout_seconds") or None,
        streaming_timeout_seconds=payload.get("streaming_timeout_seconds") or None,
        notes=str(payload.get("notes", "")),
    )


def static_file_response(settings: AppSettings, asset_path: str) -> FileResponse:
    candidate = (Path(settings.static_dir) / asset_path).resolve()
    static_root = Path(settings.static_dir).resolve()
    if static_root not in candidate.parents and candidate != static_root:
        raise HTTPException(status_code=404, detail="static asset not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="static asset not found")
    return FileResponse(candidate)


def route_payload(payload: Any) -> dict[str, object]:
    if isinstance(payload, dict):
        return payload
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if hasattr(payload, "dict"):
        return payload.dict()
    raise HTTPException(status_code=400, detail="request body must be an object")
