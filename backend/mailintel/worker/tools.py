from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from ..audit import audit_event
from ..models import MessageSummary
from ..repositories import Repository


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    handler: Callable[[dict[str, Any]], dict[str, Any]]


class RestrictedToolRegistry:
    def __init__(
        self,
        repository: Repository,
        summary_generator: Callable[[int], MessageSummary] | None = None,
    ):
        self.repository = repository
        self.summary_generator = summary_generator
        self._tools = {
            "get_message_summary": ToolDefinition(
                name="get_message_summary",
                description="Read a stored message summary and preview by internal message ID.",
                handler=self.get_message_summary,
            ),
            "search_messages": ToolDefinition(
                name="search_messages",
                description="Search indexed messages using FTS over subject, body text, and summaries.",
                handler=self.search_messages,
            ),
            "get_message_body_segment": ToolDefinition(
                name="get_message_body_segment",
                description="Read a stored body segment for one indexed message.",
                handler=self.get_message_body_segment,
            ),
            "list_interest_profiles": ToolDefinition(
                name="list_interest_profiles",
                description="List user-defined interest profiles and handling preferences.",
                handler=self.list_interest_profiles,
            ),
            "upsert_message_summary": ToolDefinition(
                name="upsert_message_summary",
                description="Update an internal derived summary record without direct SQL access.",
                handler=self.upsert_message_summary,
            ),
        }
        if self.summary_generator is not None:
            self._tools["generate_message_summary"] = ToolDefinition(
                name="generate_message_summary",
                description="Recompute and persist a grounded summary through the configured summarisation path.",
                handler=self.generate_message_summary,
            )

    def list_tools(self) -> list[dict[str, str]]:
        return [{"name": tool.name, "description": tool.description} for tool in self._tools.values()]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            raise PermissionError(f"Tool '{name}' is not available in the restricted worker")
        return self._tools[name].handler(arguments)

    def get_message_summary(self, arguments: dict[str, Any]) -> dict[str, Any]:
        row = self.repository.get_message_detail(int(arguments["message_id"]))
        if row is None:
            raise LookupError("message not found")
        return {
            "message_id": int(row["id"]),
            "subject": row["subject"],
            "summary_text": row["summary_text"],
            "preview_text": row["preview_text"],
            "importance_score": row["importance_score"],
        }

    def search_messages(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rows = self.repository.search_messages(str(arguments["query"]), limit=int(arguments.get("limit", 10)))
        return {
            "results": [
                {
                    "message_id": int(row["id"]),
                    "subject": row["subject"],
                    "summary_text": row["summary_text"],
                    "preview_text": row["preview_text"],
                }
                for row in rows
            ]
        }

    def get_message_body_segment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        detail = self.repository.get_message_detail(int(arguments["message_id"]))
        if detail is None:
            raise LookupError("message not found")
        offset = max(int(arguments.get("offset", 0)), 0)
        max_chars = max(int(arguments.get("max_chars", 4000)), 0)
        body_text = str(detail.get("body_text") or "")
        segment = body_text[offset : offset + max_chars]
        return {"message_id": int(detail["id"]), "body_segment": segment}

    def list_interest_profiles(self, arguments: dict[str, Any]) -> dict[str, Any]:
        del arguments
        rows = self.repository.list_interest_profiles()
        return {"interest_profiles": [dict(row) for row in rows]}

    def upsert_message_summary(self, arguments: dict[str, Any]) -> dict[str, Any]:
        summary = MessageSummary(
            message_id=int(arguments["message_id"]),
            summary_text=str(arguments["summary_text"]),
            importance_score=float(arguments["importance_score"]),
            rationale=str(arguments.get("rationale", "worker update")),
            confidence=float(arguments.get("confidence", 0.5)),
            model_name=str(arguments.get("model_name", "worker")),
        )
        self.repository.upsert_summary(summary)
        audit_event("worker.summary_upserted", message_id=summary.message_id, model_name=summary.model_name)
        return {"status": "ok", "message_id": summary.message_id}

    def generate_message_summary(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.summary_generator is None:
            raise PermissionError("Summary generation is not enabled in this worker")
        summary = self.summary_generator(int(arguments["message_id"]))
        return {
            "status": "ok",
            "message_id": summary.message_id,
            "summary_text": summary.summary_text,
            "importance_score": summary.importance_score,
            "rationale": summary.rationale,
            "confidence": summary.confidence,
            "model_name": summary.model_name,
        }

    def dump_capabilities(self) -> str:
        return json.dumps(self.list_tools(), indent=2)
