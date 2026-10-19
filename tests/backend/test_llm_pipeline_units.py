from __future__ import annotations

import math
import json
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from mailintel.models import ChatSource, InterestProfile, LLMPreset, MessageSummary, NormalizedMessage, TopicCluster
from mailintel.services.chat import ChatService
from mailintel.services.cleaning import CleaningConfigError, clean_email_text, load_cleaning_rules
from mailintel.services.llm import (
    ContextSnippet,
    HTTPTransport,
    LLMContentValidationError,
    LLMProviderError,
    LLMService,
    LLMSummaryError,
    PromptConfigError,
    PromptRegistry,
    QueryAnswer,
    RerankPayload,
)
from mailintel.services.pipeline import PipelineRuntime
from mailintel.services.topics import TopicClusteringService
from mailintel.settings import AppSettings


def _mock_chat_stream(monkeypatch, chunks, *, content_type="text/event-stream", status=200):
    import httpx

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for delay, chunk in chunks:
                if delay:
                    await asyncio.sleep(delay)
                yield chunk.encode()

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, headers={"content-type": content_type}, stream=Stream(), request=request)

    client_type = httpx.AsyncClient
    mock = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=mock, **kwargs))
    return requests


@pytest.mark.parametrize("provider,chunks,expected", [
    ("openai", [
        (0, 'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'),
        (0, 'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'),
        (0, 'data: {"choices":[{"delta":{"content":" world"},"finish_reason":"stop"}]}\n\n'),
    ], {"choices": [{"message": {"content": "Hello world"}, "finish_reason": "stop"}]}),
    ("ollama", [
        (0, '{"message":{"thinking":"working"},"done":false}\n'),
        (0, '{"message":{"content":"Hello"},"done":false}\n'),
        (0, '{"message":{"content":" world"},"done":true,"done_reason":"stop"}\n'),
    ], {"message": {"content": "Hello world"}, "done": True, "done_reason": "stop"}),
])
def test_chat_stream_assembles_complete_response(monkeypatch, provider, chunks, expected):
    requests = _mock_chat_stream(monkeypatch, chunks, content_type="text/event-stream" if provider == "openai" else "application/x-ndjson")
    response = HTTPTransport().post_chat_json(
        "http://provider/chat", {}, {"model": "test", "stream": True}, provider=provider,
        initial_timeout_seconds=1, streaming_timeout_seconds=1,
    )
    assert response == expected
    assert requests == [{"model": "test", "stream": True}]


@pytest.mark.parametrize("chunks,initial,gap,phase", [
    ([(0, ": heartbeat\n\n"), (0.1, 'data: {"choices":[{"delta":{"content":"late"}}]}\n\n')], 0.03, 1, "first output"),
    ([(0, 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'), (0.1, 'data: {"choices":[{"delta":{"content":"late"},"finish_reason":"stop"}]}\n\n')], 1, 0.03, "streaming gap"),
])
def test_chat_stream_timeouts_ignore_heartbeats_and_discard_partial_output(monkeypatch, chunks, initial, gap, phase):
    _mock_chat_stream(monkeypatch, chunks)
    with pytest.raises(LLMProviderError, match=phase):
        HTTPTransport().post_chat_json("http://provider/chat", {}, {"model": "test", "stream": True},
                                       provider="openai", initial_timeout_seconds=initial,
                                       streaming_timeout_seconds=gap)


def test_chat_stream_requires_completion_marker(monkeypatch):
    _mock_chat_stream(monkeypatch, [(0, 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')])
    with pytest.raises(LLMProviderError, match="invalid"):
        HTTPTransport().post_chat_json("http://provider/chat", {}, {"model": "test", "stream": True},
                                       provider="openai", initial_timeout_seconds=1,
                                       streaming_timeout_seconds=1)


def test_chat_stream_falls_back_when_provider_rejects_streaming(monkeypatch):
    import httpx

    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload["stream"]:
            return httpx.Response(422, json={"error": "stream unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client_type = httpx.AsyncClient
    mock = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=mock, **kwargs))
    result = HTTPTransport().post_chat_json("http://provider/chat", {}, {"model": "test", "stream": True},
                                            provider="openai", initial_timeout_seconds=1,
                                            streaming_timeout_seconds=1)
    assert result["choices"][0]["message"]["content"] == "ok"
    assert [item["stream"] for item in requests] == [True, False]


def test_chat_stream_activity_is_visible_only_while_request_runs(monkeypatch):
    import httpx

    sending = threading.Event()
    release = threading.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            sending.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'

    client_type = httpx.AsyncClient
    mock = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Stream(), request=request))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=mock, **kwargs))
    transport = HTTPTransport()
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(transport.post_chat_json, "http://provider/chat", {},
                                {"model": "test", "stream": True}, provider="openai", preset_id=7,
                                initial_timeout_seconds=1, streaming_timeout_seconds=1)
        try:
            assert sending.wait(1)
            for _ in range(50):
                activity = transport.stream_activity()
                if activity and activity[0]["state"] == "receiving_output":
                    break
                threading.Event().wait(0.01)
            assert len(activity) == 1
            assert activity[0]["preset_id"] == 7
            assert activity[0]["progress_chunks"] == 1
        finally:
            release.set()
        assert future.result(timeout=2)["choices"][0]["message"]["content"] == "Hello"
    assert transport.stream_activity() == []


def test_chat_stream_cancellation_releases_active_request(monkeypatch):
    import httpx

    started = threading.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            while True:
                await asyncio.sleep(0.01)
                yield b""

    client_type = httpx.AsyncClient
    mock = httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Stream(), request=request))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=mock, **kwargs))
    transport = HTTPTransport()

    def request():
        with transport.cancellation_scope("job:7"):
            return transport.post_chat_json("http://provider/chat", {}, {"model": "test", "stream": True},
                                            provider="openai", preset_id=7,
                                            initial_timeout_seconds=5, streaming_timeout_seconds=5)

    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(request)
        assert started.wait(1)
        transport.cancel("job:7")
        with pytest.raises(asyncio.CancelledError):
            future.result(timeout=2)
    assert transport.stream_activity() == []
    assert not transport._active


def test_llm_service_uses_stream_wrapper_without_partial_json(monkeypatch):
    chunks = [
        (0, 'data: {"choices":[{"delta":{"content":"{\\\"answer\\\":\\\"ok\\\"}"}}]}\n\n'),
        (0, 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'),
    ]
    requests = _mock_chat_stream(monkeypatch, chunks)
    result = LLMService(transport=HTTPTransport()).complete_strict_json(
        LLMPreset(name="stream", provider="openai", enabled=True, text_model="test",
                  initial_timeout_seconds=1, streaming_timeout_seconds=1),
        messages=[{"role": "user", "content": "Return JSON."}], max_output_tokens=100,
        json_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}},
        client_id="stream-test",
    )
    assert result == {"answer": "ok"}
    assert requests[0]["stream"] is True


@pytest.mark.parametrize("cancel_key", ["search:1", "job:1", None])
def test_http_cancel_is_search_scoped_and_covers_request_registration(monkeypatch, cancel_key):
    import httpx

    started = {key: threading.Event() for key in ("search:1", "search:2")}
    release = threading.Event()

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            started[json["key"]].set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    transport = HTTPTransport()

    def request(key):
        with transport.cancellation_scope(key.replace("search:", "job:")), transport.cancellation_scope(key):
            return transport.post_json("http://provider/v1", {}, {"key": key})

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(request, "search:1")
        second = workers.submit(request, "search:2")
        try:
            assert all(event.wait(2) for event in started.values())
            transport.cancel(cancel_key)
            with pytest.raises(asyncio.CancelledError):
                first.result(timeout=2)
            if cancel_key is None:
                with pytest.raises(asyncio.CancelledError):
                    second.result(timeout=2)
            else:
                assert not second.done()
        finally:
            release.set()
        if cancel_key is not None:
            assert second.result(timeout=2) == {"ok": True}
    assert request("search:2") == {"ok": True}

    for cancelled in ("search:3", "job:3", None):
        with transport.cancellation_scope("job:3"), transport.cancellation_scope("search:3"):
            transport.cancel(cancelled)
            with pytest.raises(asyncio.CancelledError):
                transport.post_json("http://provider/v1", {}, {})
    assert not transport._active and not transport._scopes


def test_queue_worker_registers_cancellation_before_ownership_and_ignores_lost_job():
    from mailintel.services.work_queue import WorkQueueRuntime

    calls = []

    @contextmanager
    def scope(key):
        calls.append(("scope", key))
        yield

    runtime = WorkQueueRuntime(AppSettings(database_url="postgresql://test/mailintel"),
                               SimpleNamespace(cancellation_scope=scope))
    runtime.register("test", lambda *args: pytest.fail("Paused job must not execute"))
    repository = SimpleNamespace(queue_is_owned=lambda job: calls.append(("owned", job["id"])) or False)
    runtime._run(repository, {"id": 42, "kind": "test"}, None)
    assert calls == [("scope", "job:42"), ("owned", 42)]


def _test_parse_vector(text):
    return [float(part) for part in str(text).strip("[]").split(",") if part]


def _test_cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class FakeEmbeddingTransport:
    def __init__(self):
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if "broken" in payload["model"]:
            raise RuntimeError("model unavailable")
        if url.endswith("/embeddings") and not url.endswith("/api/embeddings"):
            return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in payload["input"]]}
        return {"embedding": [0.1, 0.2, 0.3]}


class FakeChatTransport:
    def __init__(self, content: str = "The invoice deadline is Friday."):
        self.content = content
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if "broken" in payload["model"]:
            raise RuntimeError("model unavailable")
        return {"message": {"content": self.content}}


class SequenceChatTransport:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        return {"message": {"content": self.contents.pop(0)}}


class FakeOpenAIChatTransport:
    def __init__(self, content: str = "The invoice deadline is Friday."):
        self.content = content
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if "broken" in payload["model"]:
            raise RuntimeError("model unavailable")
        return {"choices": [{"message": {"content": self.content}}]}


class FakeSummaryTransport:
    def __init__(self, *, broken: bool = False, empty_content: bool = False):
        self.broken = broken
        self.empty_content = empty_content
        self.calls = []

    def post_json(self, url, headers, payload):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if self.broken:
            raise RuntimeError("model unavailable")
        if self.empty_content:
            return {
                "done": True,
                "done_reason": "stop",
                "eval_count": 800,
                "message": {"role": "assistant", "content": "", "thinking": "reasoning tokens"},
            }
        return {
            "message": {
                "content": '{"summary_text":"DMARC aggregate report for sk-cert.sk from protection.outlook.com.","rationale":"DMARC report metadata is present.","confidence":0.86}'
            }
        }


def sample_message():
    return NormalizedMessage(
        provider_message_id="m1",
        mailbox_provider_id="INBOX",
        internet_message_id=None,
        thread_id=None,
        subject="Report Domain: sk-cert.sk Submitter: protection.outlook.com",
        sender_name="DMARC",
        sender_address="dmarc@example.com",
        recipients=[],
        cc=[],
        received_at=None,
        sent_at=None,
        unread=True,
        has_attachments=True,
        importance_hint=None,
        snippet_hash="hash",
    )


def fallback_summary():
    return MessageSummary(
        message_id=None,
        summary_text="Update about report domain sk-cert.",
        importance_score=0.3,
        rationale="rules",
        confidence=0.5,
        model_name="rules/v1",
    )


def write_prompt_config(path: Path, *, user_template: str = "Value: {required}") -> Path:
    prompts = {
        name: {
            "system": f"{name} system",
            "user_template": user_template,
            "description": f"{name} description",
        }
        for name in PromptRegistry.REQUIRED_PROMPTS
    }
    path.write_text(f"prompts:\n", encoding="utf-8")
    lines = ["prompts:"]
    for name in sorted(prompts):
        lines.append(f"  {name}:")
        lines.append(f"    description: {prompts[name]['description']}")
        lines.append(f"    system: {prompts[name]['system']}")
        lines.append(f"    user_template: {prompts[name]['user_template']!r}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_clean_email_text_removes_html_template_noise():
    cleaned = clean_email_text(
        """
        <!doctype html>
        <html xmlns="urn:schemas-microsoft-com:vml">
        <style>.x { color: #fff; padding: 12px; }</style>
        <body>
          <p>Denver trip confirmation for Milan.</p>
          <p>One-click Unsubscribe: https://tracking.example/unsubscribe</p>
        </body>
        </html>
        """
    )

    assert "Denver trip confirmation" in cleaned
    assert "doctype" not in cleaned.lower()
    assert "unsubscribe" not in cleaned.lower()
    assert "schemas-microsoft" not in cleaned.lower()


def test_clean_email_text_preserves_links_and_structural_breaks():
    cleaned = clean_email_text(
        """
        <html>
          <body>
            <!-- invisible tracking comment -->
            <style>.hidden { display: none; }</style>
            <script>window.track()</script>
            <h1>Incident update</h1>
            <p>Review the <a href="https://portal.example/case?id=42&amp;source=email">case details</a>.</p>
            <ul><li>Containment complete</li><li>Follow-up required</li></ul>
          </body>
        </html>
        """
    )

    assert "Incident update" in cleaned
    assert "case details (https://portal.example/case?id=42&source=email)" in cleaned
    assert "Containment complete" in cleaned
    assert "Follow-up required" in cleaned
    assert "window.track" not in cleaned
    assert "display: none" not in cleaned
    assert "invisible tracking" not in cleaned


def test_clean_email_text_keeps_plain_urls():
    cleaned = clean_email_text("Case portal: https://portal.example/case/42")

    assert "https://portal.example/case/42" in cleaned


def test_clean_email_text_removes_external_mail_warning():
    cleaned = clean_email_text(
        """
        !!! Tento E-mail nie je z Národného bezpečnostného úradu - v prípade že nepoznáte odosielateľa alebo neviete či je obsah bezpečný, neklikajte na odkazy a neotvárajte prílohy !!!
        Report Domain: sk-cert.sk
        Submitter: protection.outlook.com
        """
    )

    assert "národného bezpečnostného úradu" not in cleaned.lower()
    assert "Report Domain: sk-cert.sk" in cleaned
    assert "Submitter: protection.outlook.com" in cleaned


def test_cleaning_rules_loader_exposes_expected_rule_groups():
    rules = load_cleaning_rules()

    assert [rule.name for rule in rules.line_drop_rules] == ["external_warning_nbu", "tracking_and_subscription_footer"]
    assert [rule.name for rule in rules.token_drop_rules] == ["html_css_template_noise"]


def test_default_body_segment_targets_8k_local_context():
    assert AppSettings().body_segment_chars == 6000


def test_cleaning_rules_loader_rejects_invalid_regex(tmp_path):
    config_path = tmp_path / "cleaning.yaml"
    config_path.write_text(
        "\n".join(
            [
                "drop_line_patterns:",
                "  - name: broken",
                "    description: broken regex",
                "    pattern: '('",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CleaningConfigError, match="Invalid regex"):
        load_cleaning_rules(config_path)


def test_prompt_registry_loads_packaged_templates():
    registry = PromptRegistry.default()
    messages = registry.render_messages(
        "summary",
        subject="Subject",
        sender_name="Sender",
        sender_address="sender@example.com",
        unread=True,
        has_attachments=False,
        match_lines="- none",
        user_context="Mailbox owner name: Test User",
        source_text="Body",
    )

    assert messages[0]["content"].startswith("You summarize email messages")
    assert "Return one valid JSON object only" in messages[1]["content"]
    assert "Mailbox owner identity/context (not message evidence; do not summarize this part)" in messages[1]["content"]
    assert "Mailbox owner name: Test User" in messages[1]["content"]
    assert "Summarize only this message evidence" in messages[1]["content"]


def test_topic_label_requires_model_result_in_incremental_queue(monkeypatch):
    service = LLMService()
    fallback = TopicCluster(id=None, topic_key="key", name="Fallback", description="Fallback",
                            key_terms=["fallback"], importance_score=0.5)
    with pytest.raises(LLMContentValidationError, match="unavailable"):
        service.label_topic_cluster(None, subjects=["Subject"], snippets=["Summary"],
                                    fallback=fallback, require_model_label=True)

    monkeypatch.setattr(service, "is_enabled", lambda _: True)
    monkeypatch.setattr(service, "_chat_completion", lambda *_args, **_kwargs: '{"description":"Only description"}')
    preset = SimpleNamespace(provider="openai", text_model="test-model")
    with pytest.raises(LLMContentValidationError, match="missing required fields"):
        service.label_topic_cluster(preset, subjects=["Subject"], snippets=["Summary"],
                                    fallback=fallback, require_model_label=True)

    monkeypatch.setattr(service, "_chat_completion", lambda *_args, **_kwargs: (
        '{"name":"Model topic","description":"Model description","key_terms":["model"],'
        '"key_entities":[],"summary":"Model summary","why_it_matters":"Model reason"}'
    ))
    labelled = service.label_topic_cluster(preset, subjects=["Subject"], snippets=["Summary"],
                                           fallback=fallback, require_model_label=True)
    assert (labelled.name, labelled.description, labelled.key_terms, labelled.model_name) == (
        "Model topic", "Model description", ["model"], "openai:test-model",
    )

    prompt = PromptRegistry.default().render_messages("topic_labeling", examples="Subject: Example")
    assert "ideally five words or less" in prompt[1]["content"]


def test_prompt_registry_renders_json_example_prompts_without_placeholder_errors():
    registry = PromptRegistry.default()

    classification_messages = registry.render_messages(
        "interest_classification",
        subject="Subject",
        sender_name="Sender",
        sender_address="sender@example.com",
        profiles_block="ID: 1\nName: Test\nDescription: Something\nKeyword hints: none",
        source_text="Body",
    )
    analysis_messages = registry.render_messages(
        "message_analysis",
        subject="Subject",
        sender_name="Sender",
        sender_address="sender@example.com",
        unread=True,
        has_attachments=False,
        profiles_block="(none configured)",
        user_context="Mailbox owner name: Test User",
        source_text="Body",
    )
    interval_messages = registry.render_messages(
        "interval_summary",
        summary_type="daily",
        interval_start_date="2026-05-06",
        period_days=1,
        mailbox_name="Inbox",
        user_context="Mailbox owner name: Test User",
        source_text="- mail_id=1; 2026-05-06; Sender; Subject: Body.",
    )
    rerank_messages = registry.render_messages(
        "search_rerank",
        query="question",
        user_context="Mailbox owner name: Test User",
        hits_payload="1. Subject: Test\nContent: Body",
    )
    answer_messages = registry.render_messages(
        "answer_query",
        query="What happened?",
        user_context="Mailbox owner name: Test User",
        context_blocks="[id=message:1 type=email date=2026-05-01 strength=weak]\nTitle: Test\nContent: The sender says I visited a friend.",
    )

    assert '{"matches":[' in classification_messages[1]["content"]
    assert "Mailbox owner identity/context (not message evidence; do not summarize this part)" in analysis_messages[1]["content"]
    assert "Mailbox owner identity/context (not message evidence; do not summarize this part)" in interval_messages[1]["content"]
    assert "Summarize and classify only this message evidence" in analysis_messages[1]["content"]
    assert "Summarize only these inputs" in interval_messages[1]["content"]
    assert '{"relevance_scores":[{"index":number,"relevance_score":number}]}' in rerank_messages[1]["content"]
    assert "not a ranking position" in rerank_messages[1]["content"]
    assert "Never return 0" in rerank_messages[1]["content"]
    assert "Do not include classifications" in rerank_messages[1]["content"]
    assert "Cite only ids from Context headers" in answer_messages[1]["content"]
    assert "Keep distinct items separate" in answer_messages[1]["content"]
    assert "If evidence is insufficient, say so" in answer_messages[1]["content"]
    assert "First-person text in emails/summaries is source wording" in answer_messages[1]["content"]


def test_topic_fallback_naming_prefers_salient_phrase_over_keyword_stitching():
    topic = TopicClusteringService(None)._build_cluster_fallback(
        "embed", 0, [{"id": 1, "subject": "Q4 finance planning review",
                      "summary_text": "Finance planning review for Q4 budget and hiring.",
                      "cleaned_text": "Finance planning review for Q4 budget and hiring."}],
    )
    assert topic.name == "Finance Planning Review"


def test_prompt_registry_rejects_malformed_yaml(tmp_path):
    config_path = tmp_path / "prompts.yaml"
    config_path.write_text("prompts: [\n", encoding="utf-8")

    with pytest.raises(PromptConfigError, match="Malformed prompt config"):
        PromptRegistry.from_yaml(config_path)


def test_prompt_registry_rejects_missing_placeholder_values(tmp_path):
    config_path = write_prompt_config(tmp_path / "prompts.yaml")
    registry = PromptRegistry.from_yaml(config_path)

    with pytest.raises(PromptConfigError, match="missing placeholder values: required"):
        registry.render_messages("summary")




@pytest.mark.parametrize("provider,window", [("ollama", 512), ("openai", 16384), ("openai", 512)])
def test_embeddings_clip_input_to_independent_embedding_context(provider, window):
    transport = FakeEmbeddingTransport()
    service = LLMService(transport=transport)
    vectors = service.embed_texts(
        LLMPreset(name="local", provider=provider, enabled=True,
                  embedding_model="working-model", text_context_window=65536,
                  embedding_context_window=window),
        texts=["á" * 40000],
    )
    assert vectors == [[0.1, 0.2, 0.3]]
    payload = transport.calls[0]["payload"]
    prompt = payload["prompt"] if provider == "ollama" else payload["input"][0]
    assert window - 33 <= len(prompt.encode("utf-8")) <= window - 32
    assert prompt.endswith("...")
    if provider == "ollama":
        assert payload["options"] == {"num_ctx": window}


def test_openai_chat_payload_does_not_include_text_context_window():
    transport = FakeOpenAIChatTransport()
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="openai", provider="openai", enabled=True, text_model="working-model", text_context_window=4096),
        query="When is the invoice deadline?",
        snippets=[ContextSnippet(subject="Invoice", content="The invoice deadline is Friday.")],
    )

    assert answer == "The invoice deadline is Friday."
    assert "context_window" not in transport.calls[0]["payload"]
    assert "num_ctx" not in transport.calls[0]["payload"]
    assert transport.calls[0]["payload"]["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in transport.calls[0]["payload"]


@pytest.mark.parametrize("provider", ["ollama", "openai"])
def test_strict_json_uses_provider_schema_constrained_generation(provider):
    transport = (
        FakeChatTransport('{"answer":"ok"}')
        if provider == "ollama"
        else FakeOpenAIChatTransport('{"answer":"ok"}')
    )
    service = LLMService(transport=transport)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer"],
        "properties": {"answer": {"type": "string"}},
    }

    result = service.complete_strict_json(
        LLMPreset(
            name="strict",
            provider=provider,
            enabled=True,
            text_model="working-model",
            text_context_window=4096,
        ),
        messages=[{"role": "user", "content": "Return JSON."}],
        max_output_tokens=100,
        json_schema=schema,
        client_id="test-strict-json",
    )

    assert result == {"answer": "ok"}
    if provider == "ollama":
        assert transport.calls[0]["payload"]["format"] == schema
    else:
        assert transport.calls[0]["payload"]["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "mailintel_strict_response",
                "strict": True,
                "schema": schema,
            },
        }


def test_openai_strict_json_reports_output_limit_truncation():
    class TruncatedTransport:
        def post_json(self, _url, _headers, _payload):
            return {"choices": [{"message": {"content": '{"answer":"unfinished'}, "finish_reason": "length"}]}
    with pytest.raises(LLMContentValidationError, match="truncated"):
        LLMService(transport=TruncatedTransport()).complete_strict_json(
            LLMPreset(name="strict", provider="openai", enabled=True, text_model="working-model"),
            messages=[{"role": "user", "content": "Return JSON."}], max_output_tokens=100, client_id="test")


def test_ollama_strict_json_reports_output_limit_truncation():
    class TruncatedTransport:
        def post_json(self, _url, _headers, _payload):
            return {
                "message": {"content": "{\"answer\":\"unfinished"},
                "done": True,
                "done_reason": "length",
            }

    service = LLMService(transport=TruncatedTransport())

    with pytest.raises(LLMContentValidationError, match="truncated"):
        service.complete_strict_json(
            LLMPreset(
                name="strict",
                provider="ollama",
                enabled=True,
                text_model="working-model",
                text_context_window=4096,
                max_output_tokens=100,
            ),
            messages=[{"role": "user", "content": "Return JSON."}],
            max_output_tokens=100,
            json_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
            client_id="test-strict-json",
        )


def test_chat_answer_context_blocks_are_source_labeled():
    transport = FakeChatTransport(
        '{"answer":"The conference registration is scheduled.","supporting_context_item_ids":["summary:10"],"maybe_relevant_context_item_ids":[]}'
    )
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="What is scheduled?",
        snippets=[
            ContextSnippet(
                subject="Daily summary (2026-05-07)",
                content="The mailbox mentions a conference registration.",
                source_type="interval_summary",
                source_id=10,
                date="2026-05-07",
                mailbox="Inbox",
                retrieval="Daily email dossier: keyword/text match",
                context_id="summary:10",
                section="Matched daily/weekly/monthly dossiers",
                source_kind="daily email dossier",
                semantic_label="Daily email dossier: keyword/text match",
                score_bucket="strong",
            )
        ],
    )

    user_message = transport.calls[0]["payload"]["messages"][1]["content"]
    assert answer == "The conference registration is scheduled."
    assert "[id=summary:10 type=summary date=2026-05-07 strength=strong]" in user_message
    assert "Title: Daily summary (2026-05-07)" in user_message
    assert "Content: The mailbox mentions a conference registration." in user_message
    assert "Context item id:" not in user_message
    assert "Source kind:" not in user_message
    assert "Retrieved by:" not in user_message
    assert "message_fts" not in user_message


def test_chat_answer_context_does_not_treat_direct_email_as_strong_by_default():
    transport = FakeChatTransport(
        '{"answer":"The evidence is partial.","supporting_context_item_ids":["message:10"],"maybe_relevant_context_item_ids":[]}'
    )
    service = LLMService(transport=transport)

    service.answer_query_with_thoughts(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="What is happening?",
        snippets=[
            ContextSnippet(
                subject="Direct email",
                content="From: Sender <sender@example.test>\n\nA direct retrieved email.",
                source_type="message",
                context_id="message:10",
                score_bucket="primary",
            )
        ],
    )

    user_message = transport.calls[0]["payload"]["messages"][1]["content"]
    assert "[id=message:10 type=email date=unknown strength=partial]" in user_message
    assert "strength=strong" not in user_message


def test_chat_answer_allows_one_word_json_but_prompt_requires_concise_grounded_answers():
    transport = FakeChatTransport(
        '{"answer":"Paphos","supporting_context_item_ids":["summary:10"],"maybe_relevant_context_item_ids":[],"evidence_sufficiency":"weak","retrieval_limitations":[]}'
    )
    service = LLMService(transport=transport)

    answer = service.answer_query_with_thoughts(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="Where am I going?",
        snippets=[
            ContextSnippet(
                subject="Daily summary",
                content="The mailbox mentions travel to Paphos.",
                source_type="interval_summary",
                context_id="summary:10",
                score_bucket="secondary",
            )
        ],
    )

    user_message = transport.calls[0]["payload"]["messages"][1]["content"]
    assert answer is not None
    assert answer.answer == "Paphos"
    assert answer.evidence_sufficiency == "weak"
    assert "Keep the answer <= 2 sentences" in user_message


def test_chat_answer_rejects_planning_json_without_user_answer():
    transport = FakeChatTransport(
        '{"task":"Answer the user question using only the supplied mail context.","context":"mail snippets","analysis":"private reasoning"}'
    )
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="FIRST emails",
        snippets=[ContextSnippet(subject="[1st-t] Request for Support", content="FIRST Teams support request.")],
    )

    assert answer is None


def test_chat_answer_extracts_answer_from_json_wrapper():
    transport = FakeChatTransport('{"answer":"There are two FIRST Teams support emails."}')
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="FIRST emails",
        snippets=[ContextSnippet(subject="[1st-t] Request for Support", content="FIRST Teams support request.")],
    )

    assert answer == "There are two FIRST Teams support emails."


def test_chat_answer_splits_leading_thoughts_from_visible_answer():
    transport = FakeChatTransport(
        "Okay, let's tackle this user question.\n\nThe user is asking about Facebook.\n</think>\n\n"
        "A Meta/Facebook ad account was reported hacked starting April 30, 2026."
    )
    service = LLMService(transport=transport)

    answer = service.answer_query_with_thoughts(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="Show me that facebook incident please?",
        snippets=[
            ContextSnippet(
                subject="Hlásenie incidentu - hacknutý Meta/Facebook reklamný účet",
                content="Unauthorized access and payments starting April 30, 2026.",
            )
        ],
    )

    assert answer is not None
    assert answer.answer == "A Meta/Facebook ad account was reported hacked starting April 30, 2026."
    assert "let's tackle" in answer.thought_text
    assert "</think>" not in answer.answer


def test_chat_answer_rejects_untagged_visible_reasoning():
    transport = FakeChatTransport("Let me analyze the context first.\n\n1. This looks relevant.")
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="What needs follow-up?",
        snippets=[ContextSnippet(subject="Project update", content="The review needs follow-up.")],
    )

    assert answer is None


def test_chat_answer_rejects_repetitive_degenerate_output():
    transport = FakeChatTransport("NatCally, " * 80)
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="Where is this event?",
        snippets=[ContextSnippet(subject="Event", content="The event is in Denver.")],
    )

    assert answer is None


def test_chat_answer_rejects_repetitive_nested_json_answer():
    transport = FakeChatTransport('{"answer":"{\\"answer\\": \\"NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally, NatCally\\"}","supporting_context_item_ids":[],"maybe_relevant_context_item_ids":[]}')
    service = LLMService(transport=transport)

    answer = service.answer_query(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        query="Where is this event?",
        snippets=[ContextSnippet(subject="Event", content="The event is in Denver.")],
    )

    assert answer is None


def test_summary_returns_llm_result_not_rule_fallback():
    transport = FakeSummaryTransport()
    service = LLMService(transport=transport)

    summary = service.summarize_message(
        LLMPreset(name="local", enabled=True, text_model="working-model", text_context_window=4096),
        message=sample_message(),
        body_text="Report Domain: sk-cert.sk\nSubmitter: protection.outlook.com",
        interest_matches=[],
        fallback=fallback_summary(),
    )

    assert summary.summary_text == "DMARC aggregate report for sk-cert.sk from protection.outlook.com."
    assert summary.model_name == "ollama:working-model"
    assert transport.calls[0]["payload"]["think"] is False
    assert transport.calls[0]["payload"]["format"] == "json"
    assert transport.calls[0]["payload"]["options"]["num_ctx"] == 4096


def test_message_analysis_clamps_source_text_to_context_budget():
    transport = FakeChatTransport('{"summary_text":"Short operational summary.","rationale":"The message has operational details.","confidence":0.8,"matches":[]}')
    service = LLMService(transport=transport)
    body_text = ("A" * 5000) + "TAIL_SHOULD_BE_CLIPPED"

    analysis = service.analyze_message(
        LLMPreset(name="local", enabled=True, text_model="working-model", text_context_window=2048, max_output_tokens=800),
        message=sample_message(),
        body_text=body_text,
        profiles=[],
        fallback=fallback_summary(),
    )

    assert analysis.summary.summary_text == "Short operational summary."
    prompt = transport.calls[0]["payload"]["messages"][1]["content"]
    assert "TAIL_SHOULD_BE_CLIPPED" not in prompt
    assert "..." in prompt


def test_summary_identity_context_is_sent_to_summary_prompts_only_as_context():
    transport = FakeSummaryTransport()
    service = LLMService(transport=transport)

    service.summarize_message(
        LLMPreset(name="local", enabled=True, text_model="working-model", text_context_window=4096),
        message=sample_message(),
        body_text="Milan received the report.",
        interest_matches=[],
        fallback=fallback_summary(),
        user_context="Mailbox owner name: Milan\nOwner aliases/usernames: milanp",
    )

    prompt = transport.calls[0]["payload"]["messages"][1]["content"]
    assert "Mailbox owner identity/context (not message evidence; do not summarize this part)" in prompt
    assert "Mailbox owner name: Milan" in prompt
    assert "Do not infer actions, places, relationships, or facts from this context" in prompt


def test_summary_failure_is_explicit():
    service = LLMService(transport=FakeSummaryTransport(broken=True))

    try:
        service.summarize_message(
            LLMPreset(name="local", enabled=True, text_model="broken-model"),
            message=sample_message(),
            body_text="Report Domain: sk-cert.sk",
            interest_matches=[],
            fallback=fallback_summary(),
        )
    except RuntimeError as exc:
        assert "model unavailable" in str(exc)
    else:
        raise AssertionError("expected provider failure")


def test_summary_failure_reports_empty_ollama_content_shape():
    service = LLMService(transport=FakeSummaryTransport(empty_content=True))

    try:
        service.summarize_message(
            LLMPreset(name="local", enabled=True, text_model="qwen3:4b"),
            message=sample_message(),
            body_text="Report Domain: sk-cert.sk",
            interest_matches=[],
            fallback=fallback_summary(),
        )
    except RuntimeError as exc:
        error = str(exc)
        assert "thinking_chars=16" in error
        assert "done_reason=stop" in error
    else:
        raise AssertionError("expected provider failure")


def test_interval_summary_requires_structured_json():
    transport = FakeChatTransport('{"entries":[{"summary":"May 6: Alice followed up on the invoice renewal.","message_ids":[101]}]}')
    service = LLMService(transport=transport)

    summary = service.summarize_interval(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        summary_type="daily",
        interval_start_date="2026-05-06",
        period_days=1,
        mailbox_name="Inbox",
        source_text="- mail_id=101; 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
    )

    assert summary == {"entries": [{"summary": "May 6: Alice followed up on the invoice renewal.", "message_ids": [101]}]}
    assert transport.calls[0]["payload"]["format"] == "json"


def test_interval_summary_prompt_requests_memo_not_email_list():
    transport = FakeChatTransport('{"entries":[{"summary":"May 6: Alice followed up on the invoice renewal.","message_ids":[101]}]}')
    service = LLMService(transport=transport)

    service.summarize_interval(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        summary_type="daily",
        interval_start_date="2026-05-06",
        period_days=1,
        mailbox_name="Inbox",
        source_text="- mail_id=101; 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
    )

    user_prompt = transport.calls[0]["payload"]["messages"][1]["content"]
    assert '{"entries":[{"summary":"short descriptive summary of the emails in the cluster","message_ids":[123]}]}' in user_prompt
    assert "A digest is a small list of broad clusters" in user_prompt


@pytest.mark.parametrize("kind", ["message", "analysis"])
def test_openai_message_summaries_request_no_reasoning(kind):
    response = '{"summary_text":"Short operational summary.","rationale":"Operational details.","confidence":0.8,"matches":[]}'
    transport = FakeOpenAIChatTransport(response)
    service = LLMService(transport=transport)
    preset = LLMPreset(
        name="llama.cpp",
        provider="openai",
        enabled=True,
        endpoint_url="http://localhost:8080/v1",
        text_model="working-model",
    )

    if kind == "message":
        service.summarize_message(
            preset,
            message=sample_message(),
            body_text="Operational details require action.",
            interest_matches=[],
            fallback=fallback_summary(),
        )
    else:
        service.analyze_message(
            preset,
            message=sample_message(),
            body_text="Operational details require action.",
            profiles=[],
            fallback=fallback_summary(),
        )

    assert transport.calls[0]["payload"]["reasoning_effort"] == "none"


def test_interval_summary_requests_openai_compatible_json_mode():
    transport = FakeOpenAIChatTransport('{"entries":[{"summary":"May 6: Alice followed up on the invoice renewal.","message_ids":[101]}]}')
    service = LLMService(transport=transport)

    summary = service.summarize_interval(
        LLMPreset(
            name="cloud",
            provider="openai-compatible",
            enabled=True,
            endpoint_url="http://example.test/v1",
            text_model="working-model",
        ),
        summary_type="daily",
        interval_start_date="2026-05-06",
        period_days=1,
        mailbox_name="Inbox",
        source_text="- mail_id=101; 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
    )

    assert summary == {"entries": [{"summary": "May 6: Alice followed up on the invoice renewal.", "message_ids": [101]}]}
    assert transport.calls[0]["payload"]["response_format"] == {"type": "json_object"}
    assert transport.calls[0]["payload"]["reasoning_effort"] == "none"


def test_interval_summary_uses_large_output_budget():
    transport = FakeChatTransport('{"entries":[{"summary":"May 6: Alice followed up on the invoice renewal.","message_ids":[101]}]}')
    service = LLMService(transport=transport)

    service.summarize_interval(
        LLMPreset(name="local", enabled=True, text_model="working-model", max_output_tokens=800),
        summary_type="monthly",
        interval_start_date="2026-02-01",
        period_days=28,
        mailbox_name="Inbox",
        source_text="- mail_id=101; 2026-02-01; Alice; Invoice renewal: Follow-up on renewal.",
    )

    assert transport.calls[0]["payload"]["options"]["num_predict"] >= 8192


def test_interval_summary_reports_truncated_json():
    service = LLMService(
        transport=FakeChatTransport('{"entries":[{"summary":"unfinished","message_ids":[13459')
    )

    with pytest.raises(LLMSummaryError, match="appears truncated"):
        service.summarize_interval(
            LLMPreset(name="local", enabled=True, text_model="working-model", max_output_tokens=800),
            summary_type="monthly",
            interval_start_date="2026-02-01",
            period_days=28,
            mailbox_name="Inbox",
            source_text="- mail_id=101; 2026-02-01; Alice; Invoice renewal: Follow-up on renewal.",
        )


def test_interval_summary_rejects_thinking_only_output():
    service = LLMService(
        transport=FakeChatTransport("Okay, let's build the interval summary. I need to inspect the input first.")
    )

    with pytest.raises(LLMSummaryError, match="valid JSON"):
        service.summarize_interval(
            LLMPreset(name="local", enabled=True, text_model="working-model"),
            summary_type="daily",
            interval_start_date="2026-05-06",
            period_days=1,
            mailbox_name="Inbox",
            source_text="- 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
        )


def test_interval_summary_rejects_unwrapped_reasoning_output():
    service = LLMService(
        transport=FakeChatTransport("We are given a list of emails for the day. We need to write a summary.")
    )

    with pytest.raises(LLMSummaryError, match="valid JSON"):
        service.summarize_interval(
            LLMPreset(name="local", enabled=True, text_model="working-model"),
            summary_type="daily",
            interval_start_date="2026-05-06",
            period_days=1,
            mailbox_name="Inbox",
            source_text="- 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
        )


def test_interval_summary_rejects_legacy_summary_string():
    service = LLMService(transport=FakeChatTransport('{"summary":"May 6: Alice followed up on the invoice renewal."}'))

    with pytest.raises(LLMSummaryError, match="entries array"):
        service.summarize_interval(
            LLMPreset(name="local", enabled=True, text_model="working-model"),
            summary_type="daily",
            interval_start_date="2026-05-06",
            period_days=1,
            mailbox_name="Inbox",
            source_text="- 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
        )


def test_interval_summary_rejects_reasoning_inside_summary_json():
    service = LLMService(
        transport=FakeChatTransport('{"entries":[{"summary":"Steps: first parse the emails, then write the summary.","message_ids":[101]}]}')
    )

    with pytest.raises(LLMSummaryError, match="reasoning text|thinking text"):
        service.summarize_interval(
            LLMPreset(name="local", enabled=True, text_model="working-model"),
            summary_type="daily",
            interval_start_date="2026-05-06",
            period_days=1,
            mailbox_name="Inbox",
            source_text="- 2026-05-06; Alice; Invoice renewal: Follow-up on renewal.",
        )


def test_llm_interest_classification_uses_profile_descriptions():
    transport = FakeChatTransport(
        '{"matches":[{"id":2,"score":0.91,"rationale":"The message invites the user to speak at a symposium."}]}'
    )
    service = LLMService(transport=transport)
    profiles = [
        InterestProfile(
            id=1,
            name="Travel",
            description="Travel bookings, airplane tickets, hotels, and reservations.",
            signals="boarding pass, hotel, reservation",
        ),
        InterestProfile(
            id=2,
            name="Conferences",
            description="Conference, event, and speaking invitations.",
            signals="conference, speaker, keynote",
        ),
    ]

    matches = service.classify_interest_profiles(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        message=NormalizedMessage(
            provider_message_id="m2",
            mailbox_provider_id="INBOX",
            internet_message_id=None,
            thread_id=None,
            subject="Invitation to speak at CERT symposium",
            sender_name="CERT-EE",
            sender_address="symposium@cert.ee",
            recipients=[],
            cc=[],
            received_at=None,
            sent_at=None,
            unread=True,
            has_attachments=False,
            importance_hint=None,
            snippet_hash="hash",
        ),
        body_text="CERT-EE invites you to speak at the annual incident response symposium.",
        profiles=profiles,
    )

    assert matches is not None
    assert [(match.interest_profile_id, match.score, match.primary_match) for match in matches] == [(2, 0.91, True)]
    prompt = transport.calls[0]["payload"]["messages"][1]["content"]
    assert "Evaluate every profile independently" in prompt
    assert "Include every profile that directly matches" in prompt
    assert "Use each profile description as the main meaning" in prompt
    assert "Keyword hints cannot turn a description mismatch into a match" in prompt
    assert "Keyword hints" in prompt


def test_llm_interest_classification_prefers_exact_name_over_wrong_id():
    transport = FakeChatTransport(
        '{"matches":[{"id":2,"name":"Incidents","score":0.95,"rationale":"This is an incident report."}]}'
    )
    service = LLMService(transport=transport)

    matches = service.classify_interest_profiles(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        message=sample_message(),
        body_text="Hlásenie kybernetického bezpečnostného incidentu.",
        profiles=[
            InterestProfile(id=2, name="Conferences", description="Conference and speaking invitations.", signals="conference"),
            InterestProfile(id=3, name="Incidents", description="Reported cyber security incidents.", signals="incident, hlásenie"),
        ],
    )

    assert matches is not None
    assert [(match.interest_profile_id, match.score) for match in matches] == [(3, 0.95)]


def test_llm_interest_classification_preserves_multiple_matches():
    transport = FakeChatTransport(
        '{"matches":['
        '{"id":2,"name":"Conferences","score":0.88,"rationale":"The message invites the user to speak at an event."},'
        '{"id":3,"name":"Travel","score":0.76,"rationale":"The same message includes hotel and flight logistics."}'
        "]}"
    )
    service = LLMService(transport=transport)

    matches = service.classify_interest_profiles(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        message=sample_message(),
        body_text="You are invited to speak at the conference. Hotel and flight details are attached.",
        profiles=[
            InterestProfile(id=2, name="Conferences", description="Conference and speaking invitations.", signals="conference, speaking"),
            InterestProfile(id=3, name="Travel", description="Travel bookings, hotels, and flights.", signals="hotel, flight"),
        ],
    )

    assert matches is not None
    assert [(match.interest_profile_id, match.primary_match) for match in matches] == [(2, True), (3, False)]


def test_llm_interest_classification_rejects_self_contradictory_match():
    transport = FakeChatTransport(
        '{"matches":[{"id":2,"name":"Conferences","score":0.45,"rationale":"The email is about a security incident investigation and does not align with conferences or speaking events."}]}'
    )
    service = LLMService(transport=transport)

    matches = service.classify_interest_profiles(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        message=sample_message(),
        body_text="Rapid7 SIEM created an investigation for disabled user network access.",
        profiles=[
            InterestProfile(id=2, name="Conferences", description="Conference and speaking invitations.", signals="conference, kyber"),
            InterestProfile(id=3, name="Incidents", description="Reported cyber security incidents.", signals="incident, hlásenie"),
        ],
    )

    assert matches == []


def test_llm_interest_classification_rejects_does_not_mention_rationale():
    transport = FakeChatTransport(
        '{"matches":[{"id":2,"name":"Conferences","score":0.75,"rationale":"The email discusses AI market structure but does not mention any conferences, events, speaking invitations, or related activities."}]}'
    )
    service = LLMService(transport=transport)

    matches = service.classify_interest_profiles(
        LLMPreset(name="local", enabled=True, text_model="working-model"),
        message=sample_message(),
        body_text="The email discusses AI model releases and market structure.",
        profiles=[
            InterestProfile(id=2, name="Conferences", description="Conference and speaking invitations.", signals="conference, event"),
        ],
    )

    assert matches == []




























def test_llm_rerank_maps_budgeted_prompt_indices_to_original_hits():
    transport = FakeChatTransport('{"relevance_scores":[{"index":2,"relevance_score":5},{"index":1,"relevance_score":3}]}')
    service = LLMService(transport=transport)
    hits = [ContextSnippet(subject=f"Hit {index}", content="Body") for index in range(10)]

    ranked = service.rerank_search_hits(
        LLMPreset(name="local", enabled=True, text_model="chat"),
        query="question",
        hits=hits,
        payload_builder=lambda _preset, _hits: service_payload([3, 7]),
    )

    assert ranked == [7, 3]


def test_llm_rerank_records_invalid_schema_diagnostics():
    transport = FakeChatTransport('{"id":"1","content":"wrong schema"}')
    service = LLMService(transport=transport)
    hits = [ContextSnippet(subject=f"Hit {index}", content="Body") for index in range(3)]
    diagnostics = []

    ranked = service.rerank_search_hits(
        LLMPreset(name="local", enabled=True, text_model="chat"),
        query="question",
        hits=hits,
        payload_builder=lambda _preset, _hits: service_payload([0, 1, 2]),
        diagnostics=diagnostics,
    )

    assert ranked is None
    assert diagnostics[0]["status"] == "invalid_or_empty"
    assert "wrong schema" in diagnostics[0]["response_sample"]


def test_llm_rerank_coerces_returned_zero_scores_to_weak_scores():
    transport = FakeChatTransport(
        '{"relevance_scores":[{"index":1,"relevance_score":0},{"index":2,"relevance_score":0}]}'
    )
    service = LLMService(transport=transport)
    hits = [ContextSnippet(subject=f"Hit {index}", content="Body") for index in range(3)]
    diagnostics = []

    ranked = service.rerank_search_hits(
        LLMPreset(name="local", enabled=True, text_model="chat"),
        query="question",
        hits=hits,
        payload_builder=lambda _preset, _hits: service_payload([0, 1, 2]),
        diagnostics=diagnostics,
    )

    assert ranked == [0, 1]
    assert diagnostics[0]["status"] == "ok"
    assert diagnostics[0]["coerced_zero_scores"] == 2
    assert diagnostics[0]["scored_original_indices"] == [
        {"index": 0, "relevance_score": 1.0},
        {"index": 1, "relevance_score": 1.0},
    ]


def test_llm_rerank_recovers_relevance_scores_from_truncated_extra_output():
    transport = FakeChatTransport(
        '{"relevance_scores":[{"index":2,"relevance_score":5},{"index":1,"relevance_score":3}'
    )
    service = LLMService(transport=transport)
    hits = [ContextSnippet(subject=f"Hit {index}", content="Body") for index in range(3)]
    diagnostics = []

    ranked = service.rerank_search_hits(
        LLMPreset(name="local", enabled=True, text_model="chat"),
        query="question",
        hits=hits,
        payload_builder=lambda _preset, _hits: service_payload([0, 1, 2]),
        diagnostics=diagnostics,
    )

    assert ranked == [1, 0]
    assert diagnostics[0]["status"] == "ok"
    assert diagnostics[0]["partial_schema_recovered"] is True


def test_llm_rerank_batches_then_sorts_scores_across_batches():
    transport = SequenceChatTransport(
        [
            '{"relevance_scores":[{"index":2,"relevance_score":4}]}',
            '{"relevance_scores":[{"index":1,"relevance_score":5}]}',
        ]
    )
    service = LLMService(transport=transport)
    hits = [ContextSnippet(subject=f"Hit {index}", content="Body") for index in range(4)]
    diagnostics = []

    def builder(_preset, batch_hits):
        if len(batch_hits) == 4:
            return [service_payload([0, 1]), service_payload([2, 3])]
        return [service_payload(list(range(len(batch_hits))))]

    ranked = service.rerank_search_hits(
        LLMPreset(name="local", enabled=True, text_model="chat"),
        query="question",
        hits=hits,
        payload_builder=builder,
        diagnostics=diagnostics,
        user_context="Mailbox owner name: Test User",
    )

    assert ranked == [2, 1]
    assert len(transport.calls) == 2
    assert diagnostics[-1]["batch"] == 2
    assert "Mailbox owner name: Test User" in transport.calls[0]["payload"]["messages"][1]["content"]


def service_payload(index_map):
    return RerankPayload(
        hits_payload="\n\n".join(
            f"{prompt_index}. id=message:{original_index} type=message date=2026-05-01\nTitle: Hit {original_index}\nContent: Body"
            for prompt_index, original_index in enumerate(index_map, start=1)
        ),
        index_map=list(index_map),
    )
