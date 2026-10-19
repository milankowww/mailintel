from __future__ import annotations

import time
from dataclasses import fields
from typing import TYPE_CHECKING

from ..models import ChatResponse, ChatSource
from ..settings import AppSettings
from .llm import LLMService
from .smart_search import SmartSearchRuntime

if TYPE_CHECKING:
    from ..repositories import Repository


class ChatService:
    """Compatibility facade: the HTTP caller waits; preset workers never wait for children."""

    def __init__(self, repository: Repository, settings: AppSettings, llm_service: LLMService | None = None):
        self.repository = repository
        self.settings = settings
        self.runtime = SmartSearchRuntime(settings, llm_service or LLMService())

    def answer_query(self, query: str) -> ChatResponse:
        search_id = self.runtime.submit(self.repository, question=query, idempotency_key=None)
        source_fields = {field.name for field in fields(ChatSource)}
        while True:
            row = self.repository.get_smart_search_job(search_id)
            self.repository.commit()
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                result = row.get("result_json") or {}
                return ChatResponse(
                    answer=str(result.get("answer") or result.get("clarification_question") or row.get("safe_error_text") or "Search cancelled."),
                    sources=[ChatSource(**{key: value for key, value in source.items() if key in source_fields}) for source in result.get("sources", [])],
                    diagnostics={"search_id": search_id, "coverage": row.get("coverage_json") or {}, "stage_trace": row.get("stage_trace_json") or {}},
                )
            time.sleep(0.25)
