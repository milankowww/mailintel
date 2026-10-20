# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import collections
import json
import logging
import asyncio
import os
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from string import Formatter
from typing import Any, Callable, Protocol, Sequence
from datetime import datetime

import httpx
import yaml

from ..domain.models import ALL_PRESET_CAPABILITIES
from ..models import InterestMatch, InterestProfile, LLMPreset, MessageSummary, NormalizedMessage, TopicCluster
from .chat_stream import ChatStreamAssembler


logger = logging.getLogger(__name__)

ANALYSIS_PROMPT_RESERVE_TOKENS = 1800
MIN_ANALYSIS_SOURCE_TOKENS = 600
ESTIMATED_CHARS_PER_TOKEN = 4
INTERVAL_SUMMARY_MIN_OUTPUT_TOKENS = int(os.environ.get("MAILINTEL_INTERVAL_SUMMARY_MIN_OUTPUT_TOKENS", "8192"))


@dataclass(slots=True)
class ContextSnippet:
    subject: str
    content: str
    source_type: str = ""
    source_id: int | None = None
    date: str = ""
    mailbox: str = ""
    retrieval: str = ""
    context_id: str = ""
    section: str = ""
    source_kind: str = ""
    semantic_label: str = ""
    score_bucket: str = ""
    message_ids: list[int] | None = None


@dataclass(slots=True)
class QueryAnswer:
    answer: str
    thought_text: str = ""
    supporting_context_item_ids: list[str] | None = None
    maybe_relevant_context_item_ids: list[str] | None = None
    evidence_sufficiency: str = ""
    retrieval_limitations: list[str] | None = None


@dataclass(slots=True)
class RerankPayload:
    hits_payload: str
    index_map: list[int]
    diagnostics: dict[str, Any] | None = None


@dataclass(slots=True)
class MessageAnalysis:
    summary: MessageSummary
    interest_matches: list[InterestMatch]


class PromptConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    system: str
    user_template: str
    description: str = ""


class PromptRegistry:
    REQUIRED_PROMPTS = {
        "summary",
        "answer_query",
        "search_query_rewrite",
        "interest_classification",
        "message_analysis",
        "topic_labeling",
        "search_rerank",
    }

    def __init__(self, templates: dict[str, PromptTemplate]):
        self._templates = templates

    @classmethod
    @lru_cache(maxsize=1)
    def default(cls) -> "PromptRegistry":
        return cls.from_yaml()

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "PromptRegistry":
        try:
            if path is None:
                raw_text = files("mailintel").joinpath("resources", "prompts.yaml").read_text(encoding="utf-8")
                source = "mailintel/resources/prompts.yaml"
            else:
                raw_text = Path(path).read_text(encoding="utf-8")
                source = str(path)
            payload = yaml.safe_load(raw_text)
        except OSError as exc:
            raise PromptConfigError(f"Unable to read prompt config: {path or 'mailintel/resources/prompts.yaml'}") from exc
        except yaml.YAMLError as exc:
            raise PromptConfigError(f"Malformed prompt config: {path or 'mailintel/resources/prompts.yaml'}") from exc

        prompts_payload = payload.get("prompts") if isinstance(payload, dict) else None
        if not isinstance(prompts_payload, dict):
            raise PromptConfigError(f"Prompt config {source} must define a top-level 'prompts' mapping")

        templates: dict[str, PromptTemplate] = {}
        for name, item in prompts_payload.items():
            if not isinstance(item, dict):
                raise PromptConfigError(f"Prompt {name!r} in {source} must be a mapping")
            system = item.get("system")
            user_template = item.get("user_template")
            if not isinstance(system, str) or not system.strip():
                raise PromptConfigError(f"Prompt {name!r} in {source} must define a non-empty system string")
            if not isinstance(user_template, str) or not user_template.strip():
                raise PromptConfigError(f"Prompt {name!r} in {source} must define a non-empty user_template string")
            templates[str(name)] = PromptTemplate(
                name=str(name),
                system=system.strip(),
                user_template=user_template.strip(),
                description=str(item.get("description") or "").strip(),
            )

        missing = sorted(cls.REQUIRED_PROMPTS - templates.keys())
        if missing:
            raise PromptConfigError(f"Prompt config {source} is missing required prompts: {', '.join(missing)}")
        return cls(templates)

    def render_messages(self, name: str, **values: object) -> list[dict[str, str]]:
        template = self._templates.get(name)
        if template is None:
            raise PromptConfigError(f"Unknown prompt template: {name}")
        values = self._with_default_values(template, values)
        self._validate_render_values(template, values)
        try:
            user_content = template.user_template.format_map(values)
        except KeyError as exc:
            raise PromptConfigError(f"Prompt {name!r} is missing placeholder value: {exc.args[0]}") from exc
        return [
            {"role": "system", "content": template.system},
            {"role": "user", "content": user_content},
        ]

    @staticmethod
    def _current_time_context() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @classmethod
    def _with_default_values(cls, template: PromptTemplate, values: dict[str, object]) -> dict[str, object]:
        required_fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(template.user_template)
            if field_name
        }
        if "now" not in required_fields or "now" in values:
            return values
        return {**values, "now": cls._current_time_context()}

    @staticmethod
    def _validate_render_values(template: PromptTemplate, values: dict[str, object]) -> None:
        required_fields = {
            field_name
            for _, field_name, _, _ in Formatter().parse(template.user_template)
            if field_name
        }
        missing = sorted(field for field in required_fields if field not in values)
        if missing:
            raise PromptConfigError(
                f"Prompt {template.name!r} is missing placeholder values: {', '.join(missing)}"
            )


SEARCH_QUERY_TOKEN_RE = re.compile(r"[^\W_]+")
SUMMARY_TOKEN_RE = re.compile(r"[^\W_]+")
SUMMARY_QUOTE_RE = re.compile(r'["“”]([^"“”]{20,})["“”]')
SEARCH_QUERY_STOPWORDS = {
    "a",
    "about",
    "ako",
    "alebo",
    "an",
    "and",
    "are",
    "as",
    "at",
    "bol",
    "bola",
    "bolo",
    "by",
    "co",
    "čo",
    "do",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "is",
    "ja",
    "je",
    "k",
    "ma",
    "má",
    "mi",
    "my",
    "na",
    "nad",
    "needs",
    "o",
    "of",
    "on",
    "or",
    "pod",
    "po",
    "pre",
    "pri",
    "s",
    "sa",
    "si",
    "som",
    "still",
    "that",
    "the",
    "this",
    "to",
    "toho",
    "tom",
    "u",
    "v",
    "what",
    "who",
    "why",
    "with",
    "z",
    "za",
    "že",
}


class LLMTransport(Protocol):
    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        ...


class LLMProviderError(RuntimeError):
    """The provider could not complete a transport request."""


class _StreamUnsupported(Exception):
    """Provider explicitly rejected streaming before starting generation."""


class LLMSummaryError(RuntimeError):
    pass


class LLMInputError(LLMSummaryError):
    pass


class LLMContentValidationError(LLMSummaryError):
    pass


class LLMPauseController:
    def __init__(self, paused: bool = False):
        self._paused = paused
        self._condition = threading.Condition()

    def is_paused(self) -> bool:
        with self._condition:
            return self._paused

    def set_paused(self, paused: bool) -> bool:
        with self._condition:
            self._paused = paused
            if not paused:
                self._condition.notify_all()
            return self._paused

    def toggle(self) -> bool:
        with self._condition:
            self._paused = not self._paused
            if not self._paused:
                self._condition.notify_all()
            return self._paused

    def wait_if_paused(self) -> None:
        with self._condition:
            while self._paused:
                self._condition.wait()


llm_pause_controller = LLMPauseController(
    paused=os.environ.get("MAILINTEL_LLM_PAUSED", "").strip().lower() in {"1", "true", "yes", "on"}
)


class HTTPTransport:
    def __init__(self, timeout_seconds: float | None = None):
        self.timeout_seconds = timeout_seconds or float(os.environ.get("MAILINTEL_LLM_TIMEOUT_SECONDS", "1800"))
        self._lock = threading.Lock()
        self._active = set()
        self._stream_activity: dict[int, dict[str, Any]] = {}
        self._scopes = set()
        self._scope = ContextVar("llm_request_scope", default=())
        self._closed = False

    @contextmanager
    def cancellation_scope(self, key):
        scope = (key, threading.Event())
        with self._lock:
            self._scopes.add(scope)
        token = self._scope.set((*self._scope.get(), scope))
        try:
            yield
        finally:
            self._scope.reset(token)
            with self._lock:
                self._scopes.discard(scope)

    def cancel(self, key=None):
        with self._lock:
            for scope in self._scopes:
                if key is None or scope[0] == key:
                    scope[1].set()
            for loop, task, scopes in self._active:
                if key is None or any(scope[0] == key for scope in scopes):
                    loop.call_soon_threadsafe(task.cancel)

    def stream_activity(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [{
                "preset_id": item["preset_id"],
                "state": item["state"],
                "progress_chunks": item["progress_chunks"],
                "elapsed_seconds": int(now - item["started_at"]),
                "quiet_seconds": int(now - item["last_progress_at"]),
            } for item in self._stream_activity.values()]

    def post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        return asyncio.run(self._post_json(url, headers, payload, timeout))

    def post_chat_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        provider: str,
        initial_timeout_seconds: float | None,
        streaming_timeout_seconds: float | None,
        preset_id: int | None = None,
    ) -> dict[str, Any]:
        initial = self.timeout_seconds if initial_timeout_seconds is None else initial_timeout_seconds
        gap = self.timeout_seconds if streaming_timeout_seconds is None else streaming_timeout_seconds
        return asyncio.run(self._post_json(url, headers, payload, self.timeout_seconds, stream_provider=provider,
                                           initial_timeout=initial, streaming_timeout=gap, preset_id=preset_id))

    async def _post_json(self, url, headers, payload, timeout, *, stream_provider=None,
                         initial_timeout=None, streaming_timeout=None, preset_id=None):
        scopes = self._scope.get()
        active = (asyncio.get_running_loop(), asyncio.current_task(), scopes)
        with self._lock:
            if self._closed or any(scope[1].is_set() for scope in scopes):
                raise asyncio.CancelledError()
            self._active.add(active)
        try:
            if stream_provider is not None:
                try:
                    return await self._stream_chat(url, headers, payload, stream_provider,
                                                   initial_timeout, streaming_timeout, preset_id)
                except _StreamUnsupported:
                    logger.info("LLM provider does not support chat streaming; using complete response: model=%s",
                                payload.get("model"))
                    payload = {**payload, "stream": False}
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url, headers={"Accept": "application/json", **headers}, json=payload,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.text
            except httpx.ResponseNotRead:
                detail = ""
            raise LLMProviderError(f"LLM request failed: {exc.response.status_code} {detail}") from exc
        except httpx.TimeoutException as exc:
            raise LLMProviderError(f"LLM request timed out after {timeout:g}s: {url}") from exc
        except httpx.RequestError as exc:
            raise LLMProviderError(f"LLM request failed: {exc}") from exc
        finally:
            with self._lock:
                self._active.discard(active)

    async def _stream_chat(self, url, headers, payload, provider, initial_timeout, streaming_timeout, preset_id):
        assembler = ChatStreamAssembler(provider)
        started = time.monotonic()
        first_output = None
        phase = "first output"
        activity_key = id(asyncio.current_task())
        with self._lock:
            self._stream_activity[activity_key] = {
                "preset_id": preset_id, "state": "waiting_for_output", "progress_chunks": 0,
                "started_at": started, "last_progress_at": started,
            }
        # asyncio.timeout measures meaningful output, including time spent waiting for headers.
        # HTTPX's read timeout is disabled so harmless SSE keepalives cannot restart it.
        try:
            async with asyncio.timeout(initial_timeout) as deadline:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, read=None)) as client:
                    async with client.stream("POST", url, headers={"Accept": "text/event-stream", **headers}, json=payload) as response:
                        if response.status_code in (400, 422):
                            detail = (await response.aread()).decode("utf-8", errors="replace").lower()
                            if "stream" in detail and any(word in detail for word in ("unsupported", "not supported", "not available", "invalid")):
                                raise _StreamUnsupported()
                        response.raise_for_status()
                        if "application/json" in response.headers.get("content-type", ""):
                            # A provider that ignores stream=true can still return a complete response.
                            complete = json.loads(await response.aread())
                            deadline.reschedule(None)
                            return complete
                        async for line in response.aiter_lines():
                            if assembler.feed_line(line):
                                now = time.monotonic()
                                with self._lock:
                                    activity = self._stream_activity[activity_key]
                                    activity.update(state="receiving_output", progress_chunks=assembler.progress_chunks,
                                                    last_progress_at=now)
                                if first_output is None:
                                    first_output = now - started
                                    logger.info("LLM stream first output: model=%s seconds=%.1f", payload.get("model"), first_output)
                                phase = "streaming gap"
                                deadline.reschedule(asyncio.get_running_loop().time() + streaming_timeout)
                            if assembler.done:
                                break
                        if not assembler.done and assembler.finish_frame():
                            phase = "streaming gap"
                        result = assembler.result()
                        deadline.reschedule(None)
                        logger.info("LLM stream complete: model=%s seconds=%.1f progress_chunks=%d",
                                    payload.get("model"), time.monotonic() - started, assembler.progress_chunks)
                        return result
        except TimeoutError as exc:
            raise LLMProviderError(f"LLM {phase} timed out after {initial_timeout if phase == 'first output' else streaming_timeout:g}s: {url}") from exc
        except ValueError as exc:
            raise LLMProviderError(f"LLM chat stream was invalid: {url}") from exc
        finally:
            with self._lock:
                self._stream_activity.pop(activity_key, None)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for loop, task, _ in self._active:
                loop.call_soon_threadsafe(task.cancel)


def llm_preset_from_row(row: object | None) -> LLMPreset | None:
    if row is None:
        return None
    return LLMPreset(
        id=int(row["id"]) if row.get("id") else None,
        name=str(row["name"]),
        provider=str(row["provider"]),
        enabled=bool(row["enabled"]),
        priority=int(row["priority"]),
        auth_mode=str(row["auth_mode"]),
        secret_locator=str(row["secret_locator"]),
        endpoint_url=str(row["endpoint_url"]),
        text_model=str(row["text_model"]),
        embedding_model=str(row["embedding_model"]),
        temperature=float(row["temperature"]),
        text_context_window=int(row.get("text_context_window") or 8192),
        embedding_context_window=int(row.get("embedding_context_window") or 8192),
        max_output_tokens=int(row["max_output_tokens"]),
        initial_timeout_seconds=float(row["initial_timeout_seconds"]) if row.get("initial_timeout_seconds") is not None else None,
        streaming_timeout_seconds=float(row["streaming_timeout_seconds"]) if row.get("streaming_timeout_seconds") is not None else None,
        notes=str(row.get("notes") or ""),
        concurrency=int(row.get("concurrency") or 1),
        capabilities=list(row["capabilities_json"]) if row.get("capabilities_json") is not None else list(ALL_PRESET_CAPABILITIES),
    )


class LLMService:
    def __init__(
        self, 
        transport: LLMTransport | None = None, 
        prompt_registry: PromptRegistry | None = None,
        pause_controller: LLMPauseController | None = None,
    ):
        self.transport = transport or HTTPTransport()
        self.prompt_registry = prompt_registry or PromptRegistry.default()
        self.pause_controller = pause_controller or llm_pause_controller

    def close(self):
        close = getattr(self.transport, "close", None)
        if close:
            close()

    def cancellation_scope(self, key):
        scope = getattr(self.transport, "cancellation_scope", None)
        return scope(key) if scope else nullcontext()

    def cancel(self, key=None):
        cancel = getattr(self.transport, "cancel", None)
        if cancel:
            cancel(key)

    def stream_activity(self) -> list[dict[str, Any]]:
        activity = getattr(self.transport, "stream_activity", None)
        return activity() if activity else []

    def _post_json(
        self,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        if isinstance(self.transport, HTTPTransport):
            return self.transport.post_json(
                url,
                headers,
                payload,
                timeout_seconds=timeout_seconds,
            )
        return self.transport.post_json(url, headers, payload)

    def _post_chat_json(self, config: LLMPreset, url: str, headers: dict[str, str],
                        payload: dict[str, Any]) -> dict[str, Any]:
        if isinstance(self.transport, HTTPTransport):
            return self.transport.post_chat_json(
                url, headers, {**payload, "stream": True},
                provider=config.provider.strip().lower(),
                preset_id=config.id,
                initial_timeout_seconds=config.initial_timeout_seconds,
                streaming_timeout_seconds=config.streaming_timeout_seconds,
            )
        return self.transport.post_json(url, headers, {**payload, "stream": False})

    @staticmethod
    def is_enabled(config: LLMPreset | None) -> bool:
        return bool(config and config.enabled and config.endpoint_url.strip())


    def complete_strict_json(
        self,
        config: LLMPreset | None,
        *,
        messages: list[dict[str, str]],
        max_output_tokens: int,
        json_schema: dict[str, Any] | None = None,
        priority: int = 0,
        client_id: str,
    ) -> dict[str, Any]:
        if not self.is_enabled(config):
            raise RuntimeError("LLM preset is disabled or missing an endpoint URL")
        strict_config = replace(
            config,
            temperature=0.0,
            max_output_tokens=max(1, min(int(max_output_tokens), int(config.max_output_tokens))),
        )
        content = self._chat_completion(
            strict_config,
            model=strict_config.text_model,
            messages=messages,
            json_mode=True,
            json_schema=json_schema,
            disable_thinking=True,
            priority=priority,
            client_id=client_id,
        )
        try:
            payload = json.loads(content.strip())
        except json.JSONDecodeError as exc:
            raise LLMContentValidationError("LLM returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise LLMContentValidationError("LLM JSON response must be an object")
        return payload

    def summarize_message(
        self,
        config: LLMPreset | None,
        *,
        message: NormalizedMessage,
        body_text: str,
        interest_matches: list[InterestMatch],
        fallback: MessageSummary,
        priority: int = 1,
        client_id: str = "default",
        user_context: str = "",
    ) -> MessageSummary:
        if not self.is_enabled(config):
            raise LLMSummaryError("LLM preset is disabled or missing an endpoint URL")

        match_lines = [f"- {match.rationale}: {match.score:.2f}" for match in interest_matches[:5]] or ["- none"]
        source_text = self._analysis_source_text(config, body_text)
        if not source_text:
            raise LLMInputError("Message has no summarizable text")

        content = self._chat_completion(
            config,
            model=config.text_model,
            json_mode=True,
            disable_thinking=True,
            messages=self.prompt_registry.render_messages(
                "summary",
                subject=message.subject,
                sender_name=message.sender_name,
                sender_address=message.sender_address,
                unread=message.unread,
                has_attachments=message.has_attachments,
                match_lines="\n".join(match_lines),
                user_context=user_context or "(none)",
                source_text=source_text,
            ),
            priority=priority,
            client_id=client_id,
        )
        parsed = self._extract_json_payload(content)

        summary_text = str(parsed.get("summary_text") or "").strip()
        if not summary_text or self._looks_extractive_summary(summary_text, source_text):
            raise LLMSummaryError("LLM summary was empty or too extractive")
        return MessageSummary(
            message_id=fallback.message_id,
            summary_text=summary_text,
            importance_score=fallback.importance_score,
            rationale=str(parsed.get("rationale") or fallback.rationale).strip(),
            confidence=self._coerce_confidence(parsed.get("confidence"), fallback.confidence),
            model_name=f"{config.provider}:{config.text_model}",
        )

    def answer_query(
        self,
        config: LLMPreset | None,
        *,
        query: str,
        snippets: Sequence[ContextSnippet],
        user_context: str = "",
        priority: int = 1,
        client_id: str = "default",
    ) -> str | None:
        answer = self.answer_query_with_thoughts(
            config,
            query=query,
            snippets=snippets,
            user_context=user_context,
            priority=priority,
            client_id=client_id,
        )
        return answer.answer if answer else None

    def answer_query_with_thoughts(
        self,
        config: LLMPreset | None,
        *,
        query: str,
        snippets: Sequence[ContextSnippet],
        user_context: str = "",
        priority: int = 1,
        client_id: str = "default",
    ) -> QueryAnswer | None:
        if not self.is_enabled(config) or not snippets:
            return None
        try:
            answer_config = replace(config, max_output_tokens=max(config.max_output_tokens, 2048))
            content = self._chat_completion(
                answer_config,
                model=answer_config.text_model,
                json_mode=True,
                messages=self.prompt_registry.render_messages(
                    "answer_query",
                    query=query,
                    user_context=user_context or "(none)",
                    context_blocks=self._format_context_blocks(snippets),
                ),
                priority=priority,
                client_id=client_id,
            )
        except PromptConfigError:
            logger.info("LLM answer prompt rendering failed", exc_info=True)
            raise
        except LLMProviderError:
            raise
        except Exception as exc:
            logger.info("LLM answer request failed: %s", exc)
            return None
        answer_text, thought_text = self._split_thinking_text(content)
        parsed_answer = self._parse_query_answer_payload(answer_text)
        answer = self._sanitize_answer_text(parsed_answer.answer if parsed_answer else answer_text)
        if not answer:
            return None
        return QueryAnswer(
            answer=answer,
            thought_text=thought_text,
            supporting_context_item_ids=parsed_answer.supporting_context_item_ids if parsed_answer else None,
            maybe_relevant_context_item_ids=parsed_answer.maybe_relevant_context_item_ids if parsed_answer else None,
            evidence_sufficiency=parsed_answer.evidence_sufficiency if parsed_answer else "",
            retrieval_limitations=parsed_answer.retrieval_limitations if parsed_answer else None,
        )

    @staticmethod
    def _format_context_blocks(snippets: Sequence[ContextSnippet]) -> str:
        blocks = []
        for index, snippet in enumerate(snippets, start=1):
            context_id = snippet.context_id or f"item:{index}"
            source_type = "summary" if snippet.source_type == "interval_summary" else "email"
            date = snippet.date or "unknown"
            strength = LLMService._context_packet_strength(snippet)
            blocks.append(
                "\n".join(
                    [
                        f"[id={context_id} type={source_type} date={date} strength={strength}]",
                        f"Title: {snippet.subject}",
                        f"Content: {snippet.content}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _context_packet_strength(snippet: ContextSnippet) -> str:
        bucket = (snippet.score_bucket or "").strip().lower()
        if bucket in {"strong", "partial", "weak", "insufficient"}:
            return bucket
        if bucket in {"primary", "secondary"}:
            return "partial"
        return "weak"

    @classmethod
    def _parse_query_answer_payload(cls, text: str) -> QueryAnswer | None:
        try:
            parsed = cls._extract_json_payload(text)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        answer = str(parsed.get("answer") or "").strip()
        if not answer:
            return None
        if answer.startswith("{"):
            nested = cls._parse_query_answer_payload(answer)
            if nested:
                return nested
        return QueryAnswer(
            answer=answer,
            supporting_context_item_ids=cls._coerce_context_item_ids(parsed.get("supporting_context_item_ids")),
            maybe_relevant_context_item_ids=cls._coerce_context_item_ids(parsed.get("maybe_relevant_context_item_ids")),
            evidence_sufficiency=str(parsed.get("evidence_sufficiency") or "").strip(),
            retrieval_limitations=cls._coerce_context_item_ids(parsed.get("retrieval_limitations")),
        )

    @staticmethod
    def _coerce_context_item_ids(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        ids = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in ids:
                ids.append(text)
        return ids

    def plan_search_strategy(
        self,
        config: LLMPreset | None,
        *,
        query: str,
        summaries_index: str,
        priority: int = 1,
        client_id: str = "default",
    ) -> dict[str, Any] | None:
        if not self.is_enabled(config):
            return None
        try:
            content = self._chat_completion(
                config,
                model=config.text_model,
                json_mode=True,
                messages=self.prompt_registry.render_messages(
                    "search_strategy",
                    query=query,
                    summaries_index=summaries_index,
                ),
                priority=priority,
                client_id=client_id,
            )
            return self._extract_json_payload(content)
        except Exception:
            return None


    def generate_search_query(
        self, 
        config: LLMPreset | None, 
        *, 
        question: str,
        priority: int = 1,
        client_id: str = "default",
    ) -> str | None:
        if not self.is_enabled(config):
            return None

        try:
            content = self._chat_completion(
                config,
                model=config.text_model,
                messages=self.prompt_registry.render_messages("search_query_rewrite", question=question),
                priority=priority,
                client_id=client_id,
            )
        except PromptConfigError:
            raise
        except Exception:
            return None

        first_line = content.strip().splitlines()[0].strip() if content.strip() else ""
        normalized = self._sanitize_search_query(first_line)
        return normalized or None



    def summarize_interval(
        self,
        config: LLMPreset | None,
        *,
        summary_type: str,
        interval_start_date: str,
        period_days: int,
        mailbox_name: str,
        source_text: str,
        priority: int = 1,
        client_id: str = "default",
        user_context: str = "",
    ) -> dict[str, Any]:
        if not self.is_enabled(config):
            raise LLMSummaryError("LLM preset is disabled or missing an endpoint URL")
        if not source_text.strip():
            raise LLMInputError("Interval has no summarizable inputs")
        interval_config = replace(
            config,
            max_output_tokens=max(config.max_output_tokens, INTERVAL_SUMMARY_MIN_OUTPUT_TOKENS),
        )
        content = self._chat_completion(
            interval_config,
            model=interval_config.text_model,
            json_mode=True,
            disable_thinking=True,
            messages=self.prompt_registry.render_messages(
                "interval_summary",
                summary_type=summary_type,
                interval_start_date=interval_start_date,
                period_days=period_days,
                mailbox_name=mailbox_name,
                user_context=user_context or "(none)",
                source_text=source_text,
            ),
            priority=priority,
            client_id=client_id,
        )
        try:
            parsed = json.loads(content.strip())
        except json.JSONDecodeError as exc:
            stripped = content.strip()
            truncated_hint = (
                " response appears truncated before a complete JSON object was emitted;"
                if self._looks_like_truncated_json(stripped, exc)
                else ""
            )
            logger.warning(
                "interval summary JSON parse failed:%s content_length=%s max_output_tokens=%s content_prefix=%.150s",
                truncated_hint,
                len(content),
                interval_config.max_output_tokens,
                stripped[:150],
            )
            raise LLMSummaryError(
                "LLM interval summary response was not valid JSON"
                + (
                    f"; response appears truncated at max_output_tokens={interval_config.max_output_tokens}"
                    if truncated_hint
                    else ""
                )
            ) from exc
        return self._normalize_interval_summary_payload(parsed)


    def _normalize_interval_summary_payload(self, parsed: Any) -> dict[str, Any]:
        entries = parsed.get("entries") if isinstance(parsed, dict) else None
        if not isinstance(entries, list):
            raise LLMContentValidationError("LLM interval summary JSON did not include an entries array")
        normalized_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            summary = " ".join(str(entry.get("summary") or "").split()).strip()
            if not summary:
                continue
            if self._looks_like_thinking_only(summary) or self._contains_interval_summary_reasoning(summary):
                raise LLMContentValidationError("LLM interval summary looked like reasoning text, not a mailbox summary")
            raw_message_ids = entry.get("message_ids")
            if not isinstance(raw_message_ids, list):
                continue
            message_ids: list[int] = []
            for value in raw_message_ids:
                try:
                    message_id = int(value)
                except (TypeError, ValueError):
                    continue
                if message_id > 0 and message_id not in message_ids:
                    message_ids.append(message_id)
            if not message_ids:
                continue
            normalized_entries.append({"summary": summary, "message_ids": message_ids})
        if not normalized_entries:
            raise LLMContentValidationError("LLM interval summary JSON did not include any valid entries")
        return {"entries": normalized_entries}

    def classify_interest_profiles(
        self,
        config: LLMPreset | None,
        *,
        message: NormalizedMessage,
        body_text: str,
        profiles: Sequence[InterestProfile],
        priority: int = 1,
        client_id: str = "default",
    ) -> list[InterestMatch] | None:
        if not self.is_enabled(config) or not profiles:
            return None
        source_text = self._analysis_source_text(config, body_text)
        if not source_text:
            return None
        profile_lines = []
        valid_ids = set()
        profiles_by_id = {}
        profile_ids_by_name = {}
        for profile in profiles:
            if profile.id is None:
                continue
            valid_ids.add(int(profile.id))
            profiles_by_id[int(profile.id)] = profile
            profile_ids_by_name[profile.name.strip().lower()] = int(profile.id)
            profile_lines.append(
                "\n".join(
                    [
                        f"ID: {profile.id}",
                        f"Name: {profile.name}",
                        f"Description: {profile.description or '(none)'}",
                        f"Keyword hints: {profile.signals or '(none)'}",
                    ]
                )
            )
        if not profile_lines:
            return None

        content = self._chat_completion(
            config,
            model=config.text_model,
            json_mode=True,
            messages=self.prompt_registry.render_messages(
                "interest_classification",
                subject=message.subject,
                sender_name=message.sender_name,
                sender_address=message.sender_address,
                profiles_block="\n\n".join(profile_lines),
                source_text=source_text,
            ),
            priority=priority,
            client_id=client_id,
        )
        parsed = self._extract_json_payload(content)
        matches: list[InterestMatch] = []
        for item in parsed.get("matches", []):
            if not isinstance(item, dict):
                continue
            try:
                profile_id = int(item.get("id"))
            except (TypeError, ValueError):
                profile_id = 0
            profile_name = str(item.get("name") or "").strip().lower()
            if profile_name in profile_ids_by_name:
                named_profile_id = profile_ids_by_name[profile_name]
                if profile_id and profile_id != named_profile_id:
                    logger.warning(
                        "LLM interest classification returned mismatched id/name: id=%s name=%r subject=%r; using name match",
                        profile_id,
                        item.get("name"),
                        message.subject,
                    )
                profile_id = named_profile_id
            if profile_id not in valid_ids:
                continue
            score = self._coerce_confidence(item.get("score"), 0.0)
            if score < 0.35:
                continue
            rationale = str(item.get("rationale") or profiles_by_id[profile_id].name).strip()
            if self._is_negative_profile_rationale(rationale):
                continue
            matches.append(InterestMatch(interest_profile_id=profile_id, score=score, rationale=rationale[:240]))
        matches.sort(key=lambda item: item.score, reverse=True)
        if matches:
            matches[0].primary_match = True
        return matches

    def analyze_message(
        self,
        config: LLMPreset | None,
        *,
        message: NormalizedMessage,
        body_text: str,
        profiles: Sequence[InterestProfile],
        fallback: MessageSummary,
        priority: int = 1,
        client_id: str = "default",
        user_context: str = "",
    ) -> MessageAnalysis:
        if not self.is_enabled(config):
            raise LLMSummaryError("LLM preset is disabled or missing an endpoint URL")
        source_text = self._analysis_source_text(config, body_text)
        if not source_text:
            raise LLMInputError("Message has no analyzable text")

        profile_lines: list[str] = []
        valid_ids: set[int] = set()
        profiles_by_id: dict[int, InterestProfile] = {}
        profile_ids_by_name: dict[str, int] = {}
        for profile in profiles:
            if profile.id is None:
                continue
            profile_id = int(profile.id)
            valid_ids.add(profile_id)
            profiles_by_id[profile_id] = profile
            profile_ids_by_name[profile.name.strip().lower()] = profile_id
            profile_lines.append(
                "\n".join(
                    [
                        f"ID: {profile.id}",
                        f"Name: {profile.name}",
                        f"Description: {profile.description or '(none)'}",
                        f"Keyword hints: {profile.signals or '(none)'}",
                    ]
                )
            )

        content = self._chat_completion(
            config,
            model=config.text_model,
            json_mode=True,
            disable_thinking=True,
            messages=self.prompt_registry.render_messages(
                "message_analysis",
                subject=message.subject,
                sender_name=message.sender_name,
                sender_address=message.sender_address,
                unread=message.unread,
                has_attachments=message.has_attachments,
                profiles_block="\n\n".join(profile_lines) if profile_lines else "(none configured)",
                user_context=user_context or "(none)",
                source_text=source_text,
            ),
            priority=priority,
            client_id=client_id,
        )
        parsed = self._extract_json_payload(content)
        summary_text = str(parsed.get("summary_text") or "").strip()
        if not summary_text or self._looks_extractive_summary(summary_text, source_text):
            raise LLMSummaryError("LLM summary was empty or too extractive")

        matches: list[InterestMatch] = []
        if valid_ids:
            for item in parsed.get("matches", []):
                if not isinstance(item, dict):
                    continue
                try:
                    profile_id = int(item.get("id"))
                except (TypeError, ValueError):
                    profile_id = 0
                profile_name = str(item.get("name") or "").strip().lower()
                if profile_name in profile_ids_by_name:
                    profile_id = profile_ids_by_name[profile_name]
                if profile_id not in valid_ids:
                    continue
                score = self._coerce_confidence(item.get("score"), 0.0)
                if score < 0.35:
                    continue
                rationale = str(item.get("rationale") or profiles_by_id[profile_id].name).strip()
                if self._is_negative_profile_rationale(rationale):
                    continue
                matches.append(InterestMatch(interest_profile_id=profile_id, score=score, rationale=rationale[:240]))
            matches.sort(key=lambda item: item.score, reverse=True)
            if matches:
                matches[0].primary_match = True

        summary = MessageSummary(
            message_id=fallback.message_id,
            summary_text=summary_text,
            importance_score=fallback.importance_score,
            rationale=str(parsed.get("rationale") or fallback.rationale).strip(),
            confidence=self._coerce_confidence(parsed.get("confidence"), fallback.confidence),
            model_name=f"{config.provider}:{config.text_model}",
        )
        return MessageAnalysis(summary=summary, interest_matches=matches)

    @staticmethod
    def _is_negative_profile_rationale(rationale: str) -> bool:
        lowered = rationale.lower()
        negative_markers = (
            "does not align",
            "doesn't align",
            "not align",
            "not directly relevant",
            "not relevant",
            "not about",
            "not related",
            "does not mention",
            "doesn't mention",
            "no mention of",
            "without mentioning",
            "does not match",
            "doesn't match",
            "not a match",
            "not fit",
        )
        return any(marker in lowered for marker in negative_markers)

    @staticmethod
    def _analysis_source_text(config: LLMPreset, body_text: str) -> str:
        source_text = body_text.strip()
        if not source_text:
            return ""
        context_window = max(int(config.text_context_window or 8192), 2048)
        max_output = max(int(config.max_output_tokens or 800), 256)
        source_tokens = max(MIN_ANALYSIS_SOURCE_TOKENS, context_window - max_output - ANALYSIS_PROMPT_RESERVE_TOKENS)
        max_chars = source_tokens * ESTIMATED_CHARS_PER_TOKEN
        if len(source_text) <= max_chars:
            return source_text
        return source_text[: max_chars - 1].rstrip() + "..."


    def embed_texts(
        self, 
        config: LLMPreset | None, 
        *, 
        texts: Sequence[str],
        priority: int = 1,
        client_id: str = "default",
        raise_on_error: bool = False,
    ) -> list[list[float]] | None:
        if not self.is_enabled(config) or not texts:
            return None
        self.pause_controller.wait_if_paused()
        
        return self._do_embed_texts(config, texts=texts, client_id=client_id, raise_on_error=raise_on_error)

    def _do_embed_texts(
        self,
        config: LLMPreset,
        *,
        texts: Sequence[str],
        client_id: str = "default",
        raise_on_error: bool = False,
    ) -> list[list[float]] | None:
        provider = config.provider.strip().lower()
        embedding_context_window = max(1, int(config.embedding_context_window))
        request_texts = [self._clip_embedding_text(text, embedding_context_window) for text in texts]
        self._log_embedding_request(
            config,
            original_texts=texts,
            request_texts=request_texts,
            client_id=client_id,
            context_window=embedding_context_window,
        )
        try:
            if provider == "ollama":
                vectors = self._ollama_embeddings(
                    config,
                    texts=request_texts,
                    embedding_context_window=embedding_context_window,
                )
            else:
                vectors = self._openai_compatible_embeddings(config, texts=request_texts)
            self._log_embedding_response(config, texts=request_texts, vectors=vectors, client_id=client_id)
            return vectors
        except Exception as exc:
            if raise_on_error and any(
                marker in str(exc).lower()
                for marker in ("exceed_context_size", "context_length_exceeded", "maximum context length")
            ):
                raise LLMInputError(str(exc)) from exc
            logger.warning(
                "LLM embedding request failed: preset=%s provider=%s endpoint=%s embedding_model=%s client_id=%s text_count=%d error=%s",
                config.name,
                config.provider,
                config.endpoint_url,
                config.embedding_model,
                client_id,
                len(texts),
                exc,
            )
            logger.debug("LLM embedding failure traceback", exc_info=True)
            if raise_on_error:
                raise
            return None

    def probe_generation(self, config: LLMPreset, *, priority: int = 1, client_id: str = "probe") -> None:
        if not self.is_enabled(config):
            raise RuntimeError("LLM preset is disabled or missing an endpoint URL")
        content = self._chat_completion(
            config,
            model=config.text_model,
            messages=[
                {"role": "system", "content": "Return only the word ok."},
                {"role": "user", "content": "ok"},
            ],
            priority=priority,
            client_id=client_id,
        )
        if not str(content or "").strip():
            raise RuntimeError("generation probe returned empty content")


    def label_topic_cluster(
        self,
        config: LLMPreset | None,
        *,
        subjects: Sequence[str],
        snippets: Sequence[str],
        fallback: TopicCluster,
        require_model_label: bool = False,
        priority: int = 1,
        client_id: str = "default",
    ) -> TopicCluster:
        if not self.is_enabled(config):
            if require_model_label:
                raise LLMContentValidationError("Topic label preset is unavailable")
            return fallback
        examples = "\n\n".join(
            f"Subject: {subject}\nSnippet: {snippet[:600]}" for subject, snippet in list(zip(subjects, snippets))[:8]
        )
        try:
            content = self._chat_completion(
                config,
                model=config.text_model,
                json_mode=True,
                messages=self.prompt_registry.render_messages("topic_labeling", examples=examples),
                priority=priority,
                client_id=client_id,
                disable_thinking=True,
            )
            parsed = self._extract_json_payload(content)
        except (PromptConfigError, LLMProviderError):
            raise
        except LLMContentValidationError:
            if require_model_label:
                raise
            return fallback
        if require_model_label:
            required = {"name", "description", "key_terms", "key_entities", "summary", "why_it_matters"}
            if required - parsed.keys():
                raise LLMContentValidationError("Topic label response is missing required fields")
            if not isinstance(parsed["name"], str) or not parsed["name"].strip():
                raise LLMContentValidationError("Topic label response has no name")
            if not isinstance(parsed["description"], str) or not parsed["description"].strip():
                raise LLMContentValidationError("Topic label response has no description")
            if not isinstance(parsed["key_terms"], list) or not isinstance(parsed["key_entities"], list):
                raise LLMContentValidationError("Topic label response has invalid term lists")
            if not isinstance(parsed["summary"], str) or not isinstance(parsed["why_it_matters"], str):
                raise LLMContentValidationError("Topic label response has invalid text fields")
        name = str(parsed.get("name") or fallback.name).strip()[:120] or fallback.name
        description = str(parsed.get("description") or fallback.description).strip() or fallback.description
        return TopicCluster(
            id=fallback.id,
            topic_key=fallback.topic_key,
            name=name,
            description=description,
            key_terms=[str(item).strip() for item in parsed.get("key_terms", fallback.key_terms) if str(item).strip()][:8],
            key_entities=[str(item).strip() for item in parsed.get("key_entities", []) if str(item).strip()][:8],
            llm_summary=str(parsed.get("summary") or "").strip(),
            why_it_matters=str(parsed.get("why_it_matters") or "").strip(),
            importance_score=fallback.importance_score,
            model_name=f"{config.provider}:{config.text_model}",
        )

    def rerank_search_hits(
        self,
        config: LLMPreset | None,
        *,
        query: str,
        hits: Sequence[ContextSnippet],
        payload_builder: Callable[[LLMPreset, Sequence[ContextSnippet]], RerankPayload | Sequence[RerankPayload]] | None = None,
        diagnostics: list[dict[str, Any]] | None = None,
        user_context: str = "",
        priority: int = 1,
        client_id: str = "default",
    ) -> list[int] | None:
        if not self.is_enabled(config) or len(hits) < 2:
            return None
        attempt: dict[str, Any] = {
            "preset_id": config.id,
            "preset_name": config.name,
            "model": config.text_model,
            "candidates_offered": len(hits),
        }
        rerank_payloads = self._build_rerank_payloads(config, hits, payload_builder)
        scored_by_index: dict[int, float] = {}
        for batch_number, rerank_payload in enumerate(rerank_payloads, start=1):
            batch_attempt = dict(attempt)
            if len(rerank_payloads) > 1:
                batch_attempt["batch"] = batch_number
                batch_attempt["batch_count"] = len(rerank_payloads)
            batch_scored = self._rerank_payload(
                config,
                query=query,
                hits=hits,
                rerank_payload=rerank_payload,
                attempt=batch_attempt,
                diagnostics=diagnostics,
                user_context=user_context,
                priority=priority,
                client_id=client_id,
            )
            for index, score in batch_scored or []:
                scored_by_index[index] = max(scored_by_index.get(index, 0.0), score)
        ranked = [
            index
            for index, score in sorted(
                scored_by_index.items(),
                key=lambda item: (-item[1], item[0]),
            )
            if score > 0
        ]
        return ranked or None

    def _build_rerank_payloads(
        self,
        config: LLMPreset,
        hits: Sequence[ContextSnippet],
        payload_builder: Callable[[LLMPreset, Sequence[ContextSnippet]], RerankPayload | Sequence[RerankPayload]] | None,
    ) -> list[RerankPayload]:
        if payload_builder is None:
            return [
                RerankPayload(
                    hits_payload="\n".join(
                        "\n".join(
                            [
                                f"{index}. id={hit.context_id or f'item:{index}'} type={hit.source_type or 'unknown'} date={hit.date or 'unknown'}",
                                f"Title: {hit.subject}",
                                f"Content: {hit.content[:500]}",
                            ]
                        )
                        for index, hit in enumerate(hits, start=1)
                    ),
                    index_map=list(range(len(hits))),
                    diagnostics=None,
                )
            ]
        payloads = payload_builder(config, hits)
        if isinstance(payloads, RerankPayload):
            return [payloads]
        return [payload for payload in payloads if isinstance(payload, RerankPayload)]

    def _rerank_payload(
        self,
        config: LLMPreset,
        *,
        query: str,
        hits: Sequence[ContextSnippet],
        rerank_payload: RerankPayload,
        attempt: dict[str, Any],
        diagnostics: list[dict[str, Any]] | None,
        user_context: str = "",
        priority: int = 1,
        client_id: str = "default",
    ) -> list[tuple[int, float]] | None:
        payload = rerank_payload.hits_payload
        index_map = list(rerank_payload.index_map)
        if rerank_payload.diagnostics:
            attempt.update(rerank_payload.diagnostics)
        attempt["candidates_included"] = len(index_map)
        attempt["payload_chars"] = len(payload)
        if len(index_map) < 2:
            attempt["status"] = "not_enough_budgeted_hits"
            if diagnostics is not None:
                diagnostics.append(attempt)
            return None
        content = ""
        partial_relevance_scores: list[dict[str, Any]] | None = None
        try:
            rerank_config = replace(config, max_output_tokens=max(config.max_output_tokens, 2048))
            content = self._chat_completion(
                rerank_config,
                model=rerank_config.text_model,
                json_mode=True,
                messages=self.prompt_registry.render_messages(
                    "search_rerank",
                    query=query,
                    user_context=user_context or "(none)",
                    hits_payload=payload,
                ),
                priority=priority,
                client_id=client_id,
            )
            try:
                parsed = self._extract_json_payload(content)
            except Exception:
                partial_relevance_scores = self._extract_relevance_scores_from_text(content)
                if partial_relevance_scores is None:
                    raise
                parsed = {"relevance_scores": partial_relevance_scores}
                attempt["partial_schema_recovered"] = True
        except PromptConfigError:
            raise
        except Exception as exc:
            attempt["status"] = "failed"
            attempt["error"] = str(exc)[:300]
            attempt["response_sample"] = str(content or "")[:500]
            if diagnostics is not None:
                diagnostics.append(attempt)
            return None
        scored: dict[int, float] = {}
        coerced_zero_scores = 0
        for item in parsed.get("relevance_scores", []):
            if not isinstance(item, dict):
                continue
            try:
                prompt_index = int(item.get("index")) - 1
                score_value = item.get("relevance_score", item.get("relevance", item.get("score")))
                score = float(score_value)
            except (TypeError, ValueError):
                continue
            if score <= 0:
                coerced_zero_scores += 1
                score = 1.0
            score = max(1.0, min(5.0, score))
            if score > 0 and 0 <= prompt_index < len(index_map):
                original_index = index_map[prompt_index]
                if 0 <= original_index < len(hits):
                    scored[original_index] = max(scored.get(original_index, 0.0), score)
        if not scored:
            attempt["status"] = "invalid_or_empty"
            attempt["response_sample"] = str(content or "")[:500]
            attempt["parsed_keys"] = sorted(str(key) for key in parsed.keys()) if isinstance(parsed, dict) else []
            if diagnostics is not None:
                diagnostics.append(attempt)
            return None
        attempt["status"] = "ok"
        if coerced_zero_scores:
            attempt["coerced_zero_scores"] = coerced_zero_scores
        attempt["scored_original_indices"] = [
            {"index": index, "relevance_score": score}
            for index, score in sorted(scored.items(), key=lambda item: (-item[1], item[0]))
        ]
        if diagnostics is not None:
            diagnostics.append(attempt)
        return list(scored.items())

    @staticmethod
    def _extract_relevance_scores_from_text(content: str) -> list[dict[str, Any]] | None:
        array_match = re.search(
            r'"relevance_scores"\s*:\s*\[(.*)',
            content or "",
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not array_match:
            return None
        values: list[dict[str, Any]] = []
        for item_match in re.finditer(
            r'\{[^{}]*"index"\s*:\s*(-?\d+)[^{}]*"(?:relevance_score|relevance|score)"\s*:\s*(-?\d+(?:\.\d+)?)[^{}]*\}',
            array_match.group(1),
            flags=re.IGNORECASE | re.DOTALL,
        ):
            values.append({"index": int(item_match.group(1)), "relevance_score": float(item_match.group(2))})
        return values or None




    @staticmethod
    def _split_thinking_text(content: str) -> tuple[str, str]:
        text = content.strip()
        if not text:
            return "", ""
        thought_parts: list[str] = []

        def capture_block(match: re.Match[str]) -> str:
            thought_parts.append(match.group(1).strip())
            return "\n"

        text = re.sub(r"(?is)<think>\s*(.*?)\s*</think>", capture_block, text).strip()
        close_match = re.search(r"(?is)</think>", text)
        if close_match:
            before = text[: close_match.start()].strip()
            after = text[close_match.end() :].strip()
            if before:
                thought_parts.insert(0, re.sub(r"(?is)^<think>\s*", "", before).strip())
            text = after
        thought_text = "\n\n".join(part for part in thought_parts if part)
        return text, thought_text

    @staticmethod
    def _sanitize_answer_text(content: str) -> str | None:
        text = content.strip()
        if not text:
            return None
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        if text.startswith("{"):
            try:
                parsed = LLMService._extract_json_payload(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                lowered = text[:1200].lower()
                if any(f'"{key}"' in lowered for key in ("task", "context", "analysis")):
                    return None
                return text
            for key in ("answer", "final", "response"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    text = value.strip()
                    break
            if any(key in parsed for key in ("task", "context", "analysis")):
                return None
        if LLMService._looks_like_thinking_only(text):
            return None
        if LLMService._looks_repetitive_answer(text):
            return None
        return text

    @staticmethod
    def _looks_like_thinking_only(text: str) -> bool:
        lowered = text.strip().lower()
        if not lowered:
            return True
        thinking_markers = (
            "okay, let's",
            "let me analyze",
            "i'll go through",
            "let's tackle",
            "the user is asking",
            "we are given",
            "we need answer",
            "we need to",
            "i need to",
            "i should",
            "need craft",
            "first,",
            "now craft",
        )
        return any(lowered.startswith(marker) for marker in thinking_markers)

    @staticmethod
    def _looks_repetitive_answer(text: str) -> bool:
        tokens = re.findall(r"[^\W_]+", text.lower())
        if len(tokens) < 20:
            return False
        token_counts = collections.Counter(tokens)
        if token_counts.most_common(1)[0][1] / len(tokens) >= 0.45:
            return True
        normalized_phrases = re.findall(r"[^,.;:\n]+", text.lower())
        phrases = [phrase.strip() for phrase in normalized_phrases if len(phrase.strip()) >= 4]
        if len(phrases) >= 8 and collections.Counter(phrases).most_common(1)[0][1] / len(phrases) >= 0.5:
            return True
        return False

    @staticmethod
    def _contains_interval_summary_reasoning(text: str) -> bool:
        lowered = text.lower()
        reasoning_markers = (
            "we are given",
            "steps:",
            "approach:",
            "let's parse",
            "<think>",
            "</think>",
        )
        return any(marker in lowered for marker in reasoning_markers)

    @staticmethod
    def _looks_like_truncated_json(text: str, exc: json.JSONDecodeError) -> bool:
        stripped = text.strip()
        if not stripped:
            return False
        if exc.pos < max(len(stripped) - 32, 0):
            return False
        if stripped[0] not in "{[":
            return False
        if stripped[-1] not in "}]" or stripped.count("{") > stripped.count("}") or stripped.count("[") > stripped.count("]"):
            return True
        return False

    @staticmethod
    def _sanitize_search_query(text: str) -> str:
        tokens = SEARCH_QUERY_TOKEN_RE.findall(text.lower())
        normalized_tokens: list[str] = []
        seen_tokens: set[str] = set()
        for token in tokens:
            if len(token) <= 1 or token in SEARCH_QUERY_STOPWORDS:
                continue
            if token in seen_tokens:
                continue
            normalized_tokens.append(token)
            seen_tokens.add(token)
        return " ".join(normalized_tokens)

    @classmethod
    def _looks_extractive_summary(cls, summary_text: str, source_text: str) -> bool:
        normalized_summary = cls._normalize_summary_text(summary_text)
        normalized_source = cls._normalize_summary_text(source_text)
        if not normalized_summary or not normalized_source:
            return False
        if len(normalized_summary) >= 32 and normalized_summary in normalized_source:
            return True
        for match in SUMMARY_QUOTE_RE.finditer(summary_text):
            normalized_quote = cls._normalize_summary_text(match.group(1))
            if len(normalized_quote) >= 24 and normalized_quote in normalized_source:
                return True
        summary_tokens = cls._summary_tokens(summary_text)
        source_tokens = cls._summary_tokens(source_text)
        if len(summary_tokens) < 6 or not source_tokens:
            return False
        longest_run = cls._longest_common_token_run(summary_tokens, source_tokens)
        return longest_run >= max(7, int(len(summary_tokens) * 0.75 + 0.5))

    @staticmethod
    def _normalize_summary_text(text: str) -> str:
        return " ".join(SUMMARY_TOKEN_RE.findall(text.lower()))

    @staticmethod
    def _summary_tokens(text: str) -> list[str]:
        return SUMMARY_TOKEN_RE.findall(text.lower())

    @staticmethod
    def _longest_common_token_run(left: list[str], right: list[str]) -> int:
        positions: dict[str, list[int]] = {}
        for index, token in enumerate(right):
            positions.setdefault(token, []).append(index)
        best = 0
        previous: dict[int, int] = {}
        for token in left:
            current: dict[int, int] = {}
            for index in positions.get(token, []):
                current[index] = previous.get(index - 1, 0) + 1
                if current[index] > best:
                    best = current[index]
            previous = current
        return best

    def _chat_completion(
        self, 
        config: LLMPreset, 
        *, 
        model: str, 
        messages: list[dict[str, str]], 
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
        disable_thinking: bool = False,
        priority: int = 1,
        client_id: str = "default",
    ) -> str:
        self.pause_controller.wait_if_paused()
        return self._do_chat_completion(
            config,
            model=model,
            messages=messages,
            json_mode=json_mode,
            json_schema=json_schema,
            disable_thinking=disable_thinking,
        )


    def _do_chat_completion(
        self,
        config: LLMPreset,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
        disable_thinking: bool = False,
    ) -> str:
        provider = config.provider.strip().lower()
        self._log_chat_request(config, model=model, messages=messages, json_mode=json_mode)
        if provider == "ollama":
            content = self._ollama_chat_completion(
                config,
                model=model,
                messages=messages,
                json_mode=json_mode,
                json_schema=json_schema,
            )
        else:
            content = self._openai_compatible_chat_completion(
                config,
                model=model,
                messages=messages,
                json_mode=json_mode,
                json_schema=json_schema,
                disable_thinking=disable_thinking,
            )
        self._log_chat_response(config, model=model, content=content)
        return content

    @staticmethod
    def _log_chat_request(
        config: LLMPreset,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool,
    ) -> None:
        logger.info(
            "LLM chat request: preset=%s [%s] provider=%s model=%s "
            "json_mode=%s message_count=%d input_chars=%d",
            config.name,
            config.id,
            config.provider,
            model,
            json_mode,
            len(messages),
            sum(len(str(message.get("content") or "")) for message in messages),
        )

    @staticmethod
    def _log_chat_response(config: LLMPreset, *, model: str, content: str) -> None:
        logger.info(
            "LLM chat response: preset=%s [%s] provider=%s model=%s output_chars=%d",
            config.name,
            config.id,
            config.provider,
            model,
            len(content),
        )

    @staticmethod
    def _log_embedding_request(
        config: LLMPreset,
        *,
        original_texts: Sequence[str],
        request_texts: Sequence[str],
        client_id: str,
        context_window: int | None,
    ) -> None:
        logger.info(
            "LLM embedding request: preset=%s [%s] provider=%s model=%s "
            "client_id=%s context_window=%s text_count=%d input_chars=%d clipped_count=%d",
            config.name,
            config.id,
            config.provider,
            config.embedding_model,
            client_id,
            context_window or "",
            len(request_texts),
            sum(len(text) for text in request_texts),
            sum(1 for original, request in zip(original_texts, request_texts) if original != request),
        )

    @staticmethod
    def _log_embedding_response(
        config: LLMPreset,
        *,
        texts: Sequence[str],
        vectors: Sequence[Sequence[float]],
        client_id: str,
    ) -> None:
        dimensions = [len(vector) for vector in vectors]
        logger.info(
            "\n=== LLM embedding response ===\n"
            "preset: %s [%s]\n"
            "provider: %s\n"
            "model: %s\n"
            "client_id: %s\n"
            "text_count: %d\n"
            "vector_count: %d\n"
            "dimensions: %s\n"
            "=== end LLM embedding response ===",
            config.name,
            config.id,
            config.provider,
            config.embedding_model,
            client_id,
            len(texts),
            len(vectors),
            dimensions,
        )

    def _openai_compatible_chat_completion(
        self,
        config: LLMPreset,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
        disable_thinking: bool = False,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_output_tokens,
        }
        if disable_thinking:
            payload["reasoning_effort"] = "none"
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "mailintel_strict_response",
                    "strict": True,
                    "schema": json_schema,
                },
            }
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = self._post_chat_json(
            config,
            self._build_endpoint(config.endpoint_url, "/chat/completions"),
            self._authorization_headers(config),
            payload,
        )
        choices = response.get("choices") or []
        if not choices:
            raise RuntimeError("LLM response did not include choices")
        finish_reason = choices[0].get("finish_reason")
        logger.info("LLM completion finished: preset=%s model=%s reason=%s output_limit=%s",
                    config.id, model, finish_reason, config.max_output_tokens)
        if json_mode and finish_reason == "length":
            raise LLMContentValidationError(
                f"LLM structured output was truncated at max_output_tokens={config.max_output_tokens}")
        message = choices[0].get("message") or {}
        text = self._extract_text_content(message.get("content"))
        if not text:
            raise RuntimeError("LLM response did not include message content")
        return text

    def _ollama_chat_completion(
        self,
        config: LLMPreset,
        *,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": os.environ.get("MAILINTEL_OLLAMA_CHAT_KEEP_ALIVE", "30m"),
            "options": {
                "temperature": config.temperature,
                "num_ctx": config.text_context_window,
                "num_predict": config.max_output_tokens,
            },
        }
        if json_schema is not None:
            payload["format"] = json_schema
        elif json_mode:
            payload["format"] = "json"
        response = self._post_chat_json(
            config,
            self._build_endpoint(config.endpoint_url, "/api/chat"),
            self._authorization_headers(config),
            payload,
        )
        if (
            (json_schema is not None or json_mode)
            and str(response.get("done_reason") or "").lower() in {"length", "max_tokens"}
        ):
            raise LLMContentValidationError(
                f"LLM structured output was truncated at max_output_tokens={config.max_output_tokens}"
            )
        message = response.get("message") or {}
        text = self._extract_text_content(message.get("content"))
        if not text:
            raise RuntimeError(f"LLM response did not include message content ({self._response_diagnostics(response)})")
        return text

    def _openai_compatible_embeddings(self, config: LLMPreset, *, texts: Sequence[str]) -> list[list[float]]:
        response = self._post_json(
            self._build_endpoint(config.endpoint_url, "/embeddings"),
            self._authorization_headers(config),
            {"model": config.embedding_model, "input": list(texts)},
            timeout_seconds=self._embedding_timeout_seconds(),
        )
        data = response.get("data") or []
        vectors = [item.get("embedding") for item in data if isinstance(item, dict)]
        if len(vectors) != len(texts):
            raise RuntimeError("embedding response count mismatch")
        return [[float(value) for value in vector] for vector in vectors]

    @staticmethod
    def _embedding_timeout_seconds() -> float:
        return float(os.environ.get("MAILINTEL_EMBEDDING_TIMEOUT_SECONDS", os.environ.get("MAILINTEL_LLM_TIMEOUT_SECONDS", "1800")))

    @staticmethod
    def _clip_embedding_text(text: str, context_window: int) -> str:
        # One UTF-8 byte per token is conservative for byte-fallback tokenizers.
        # Leave room for the provider's special tokens and the clipping suffix.
        max_bytes = max(1, context_window - 32)
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        if max_bytes <= len("..."):
            return encoded[:max_bytes].decode("utf-8", errors="ignore")
        return encoded[: max_bytes - len("...")].decode("utf-8", errors="ignore").rstrip() + "..."

    def _ollama_embeddings(
        self,
        config: LLMPreset,
        *,
        texts: Sequence[str],
        embedding_context_window: int,
    ) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            response = self._post_json(
                self._build_endpoint(config.endpoint_url, "/api/embeddings"),
                self._authorization_headers(config),
                {
                    "model": config.embedding_model,
                    "prompt": text,
                    "keep_alive": os.environ.get("MAILINTEL_OLLAMA_EMBEDDING_KEEP_ALIVE", "10m"),
                    "options": {"num_ctx": embedding_context_window},
                },
                timeout_seconds=self._embedding_timeout_seconds(),
            )
            vector = response.get("embedding")
            if not isinstance(vector, list):
                raise RuntimeError("embedding response did not include embedding")
            vectors.append([float(value) for value in vector])
        return vectors

    @staticmethod
    def _build_endpoint(base_url: str, suffix: str) -> str:
        normalized_base = base_url.rstrip("/")
        normalized_suffix = suffix.lstrip("/")
        if normalized_base.endswith(normalized_suffix):
            return normalized_base
        return f"{normalized_base}/{normalized_suffix}"

    @staticmethod
    def _authorization_headers(config: LLMPreset) -> dict[str, str]:
        token = ""
        if config.auth_mode == "env" and config.secret_locator:
            token = os.environ.get(config.secret_locator, "").strip()
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    @staticmethod
    def _extract_json_payload(content: str) -> dict[str, Any]:
        start = content.find("{")
        end = content.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM response did not contain JSON; content_sample={LLMService._diagnostic_sample(content)!r}")
        return json.loads(content[start : end + 1])

    @staticmethod
    def _extract_text_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [str(item.get("text", "")) for item in content if isinstance(item, dict)]
            return "".join(parts).strip()
        return ""

    @staticmethod
    def _response_diagnostics(response: dict[str, Any]) -> str:
        message = response.get("message")
        message_keys = sorted(message.keys()) if isinstance(message, dict) else []
        thinking = message.get("thinking") if isinstance(message, dict) else None
        thinking_length = len(thinking) if isinstance(thinking, str) else 0
        fields = [
            f"response_keys={sorted(response.keys())}",
            f"message_keys={message_keys}",
            f"done={response.get('done')}",
            f"done_reason={response.get('done_reason')}",
            f"eval_count={response.get('eval_count')}",
            f"thinking_chars={thinking_length}",
        ]
        return ", ".join(fields)

    @staticmethod
    def _diagnostic_sample(text: str, limit: int = 300) -> str:
        return " ".join(text.split())[:limit]

    @staticmethod
    def _coerce_confidence(value: Any, fallback: float) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return fallback
        return min(max(numeric, 0.0), 1.0)
