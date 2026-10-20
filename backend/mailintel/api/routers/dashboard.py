# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from fastapi import FastAPI, HTTPException

from ...auth import secured_route
from ...connectors.base import ConnectorError
from ..dependencies import AppContext, connector_http_exception, date_range_params, summary_period_days
from ..dependencies import route_payload
from ..schemas import ChatPayload, SummaryListPayload, SummaryRebuildPayload, TopicRenamePayload
from ..serializers import row_to_dict
from ..schemas.requests import MessageStarPayload


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository
    dashboard_service = ctx.dashboard_service
    chat_service = ctx.chat_service

    @secured_route(app, "GET", "/api/message-stars", capability="mail.read")
    def message_stars():
        return {"stars": repository.list_message_stars()}

    @secured_route(app, "GET", "/api/starred", capability="mail.read")
    def starred(limit: int = 50, offset: int = 0):
        if not 1 <= limit <= 200 or offset < 0:
            raise HTTPException(status_code=400, detail="Invalid pagination")
        rows = repository.list_starred_messages(limit + 1, offset)
        return {"messages": [row_to_dict(row) for row in rows[:limit]], "has_more": len(rows) > limit}

    @secured_route(app, "PUT", "/api/messages/{message_id}/star", capability="mail.write")
    def set_star(message_id: int, payload: MessageStarPayload):
        row = repository.set_message_star(message_id, payload.star)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        return row

    @secured_route(app, "GET", "/api/triage", capability="mail.read")
    def triage(limit: int = 50, from_date: str | None = None, to_date: str | None = None):
        parsed_from, parsed_to = date_range_params(from_date, to_date)
        payload = dashboard_service.triage_overview(
            limit=limit, from_date=parsed_from, to_date=parsed_to,
        )
        return {
            "triage": [row_to_dict(row) for row in payload.get("triage", [])],
            "interest_profiles": [
                {
                    **row_to_dict(profile),
                    "matched_messages": [row_to_dict(message) for message in profile.get("matched_messages", [])],
                }
                for profile in payload.get("interest_profiles", [])
            ],
        }

    @secured_route(app, "POST", "/api/summaries", capability="mail.read")
    def list_summaries(payload: SummaryListPayload):
        payload = route_payload(payload)
        raw_requests = payload.get("requests", [])
        if not isinstance(raw_requests, list):
            raise HTTPException(status_code=400, detail="requests must be a list")
        if len(raw_requests) > 100:
            raise HTTPException(status_code=400, detail="requests must contain at most 100 intervals")
        mailbox_ids: list[int] | None = None
        if "mailbox_ids" in payload and payload.get("mailbox_ids") not in (None, ""):
            raw_mailbox_ids = payload.get("mailbox_ids")
            if not isinstance(raw_mailbox_ids, list):
                raise HTTPException(status_code=400, detail="mailbox_ids must be a list")
            try:
                mailbox_ids = [int(item) for item in raw_mailbox_ids]
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="mailbox_ids must contain integers") from exc
        interval_requests: list[tuple[date, int]] = []
        for item in raw_requests:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="each request must be an object")
            try:
                start_date = date.fromisoformat(str(item.get("start_date", "")))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD") from exc
            duration = str(item.get("duration", "")).strip().lower()
            if duration == "week" and start_date.weekday() != 0:
                raise HTTPException(status_code=400, detail="week summaries must start on Monday")
            interval_requests.append((start_date, summary_period_days(start_date, duration)))
        return {"summaries": [row_to_dict(row) for row in repository.list_interval_summaries(interval_requests, mailbox_ids)]}

    @secured_route(app, "POST", "/api/summaries/request-rebuild", capability="sync.run")
    def request_summary_rebuild(payload: SummaryRebuildPayload):
        payload = route_payload(payload)
        try:
            start_date = date.fromisoformat(str(payload.get("start_date", "")))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="start_date must be YYYY-MM-DD") from exc
        duration = str(payload.get("duration", "")).strip().lower()
        if duration == "week" and start_date.weekday() != 0:
            raise HTTPException(status_code=400, detail="week summaries must start on Monday")
        period_days = summary_period_days(start_date, duration)
        summary_type = {"day": "daily", "week": "weekly", "month": "monthly"}[duration]
        rows = repository.request_interval_summary_rebuild(start_date, period_days, summary_type)
        return {
            "status": "ok",
            "requested": len(rows),
            "summary_ids": [int(row["id"]) for row in rows],
        }

    @secured_route(app, "GET", "/api/messages/{message_id}", capability="mail.read")
    def get_message(message_id: int):
        row = repository.get_message_detail(message_id)
        if row is None:
            raise HTTPException(status_code=404, detail="message not found")
        return {"message": row_to_dict(row)}

    @secured_route(app, "GET", "/api/topics", capability="mail.read")
    def list_topics(from_date: str | None = None, to_date: str | None = None,
                    limit: int = 200, offset: int = 0):
        if not 1 <= limit <= 10000 or offset < 0:
            raise HTTPException(status_code=400, detail="limit must be 1–10000 and offset nonnegative")
        parsed_from, parsed_to = date_range_params(from_date, to_date)
        rows = repository.list_dashboard_topics(
            limit=limit, offset=offset, from_date=parsed_from, to_date=parsed_to,
        )
        return {"topics": [row_to_dict(row) for row in rows]}

    @secured_route(app, "GET", "/api/topics/{topic_id}", capability="mail.read")
    def topic_detail(topic_id: int):
        row = repository.get_topic_detail(topic_id)
        if row is None:
            raise HTTPException(status_code=404, detail="topic not found")
        return {"topic": row_to_dict(row)}

    @secured_route(app, "GET", "/api/topics/{topic_id}/messages", capability="mail.read")
    def topic_messages(topic_id: int, limit: int = 50, offset: int = 0,
                       from_date: str | None = None, to_date: str | None = None):
        if not 1 <= limit <= 100 or offset < 0:
            raise HTTPException(status_code=400, detail="limit must be 1–100 and offset nonnegative")
        parsed_from, parsed_to = date_range_params(from_date, to_date)
        rows = repository.list_messages_for_topic(
            topic_id, limit=limit, offset=offset, from_date=parsed_from, to_date=parsed_to,
        )
        return {"messages": [row_to_dict(row) for row in rows]}

    @secured_route(app, "PATCH", "/api/topics/{topic_id}", capability="settings.write")
    def rename_topic(topic_id: int, payload: TopicRenamePayload):
        name = str(route_payload(payload).get("name") or "").strip()
        if not name or len(name) > 160:
            raise HTTPException(status_code=400, detail="name must contain 1–160 characters")
        row = repository.rename_topic(topic_id, name)
        if row is None:
            raise HTTPException(status_code=404, detail="topic not found")
        return {"topic": row_to_dict(row)}

    @secured_route(app, "POST", "/api/topics/{topic_id}/relabel", capability="settings.write")
    def request_topic_relabel(topic_id: int):
        row = repository.get_topic_detail(topic_id)
        if row is None:
            raise HTTPException(status_code=404, detail="topic not found")
        job = repository.queue_enqueue(
            "relabel_topic", f"topic-relabel:{topic_id}",
            {"topic_id": topic_id, "expected_name": row["name"],
             "embedding_model": row["embedding_model"]}, priority=1,
        )
        return {"status": "queued", "job_id": int(job["id"])}

    @secured_route(app, "POST", "/api/chat", capability="mail.read")
    def chat(payload: ChatPayload):
        payload = route_payload(payload)
        query = str(payload["query"])
        try:
            response = chat_service.answer_query(query)
        except (ConnectorError, KeyError, TypeError, ValueError) as exc:
            raise connector_http_exception(exc) from exc
        return {
            "answer": response.answer,
            "thought_text": response.thought_text,
            "diagnostics": getattr(response, "diagnostics", {}),
            "sources": [asdict(source) for source in response.sources],
        }
