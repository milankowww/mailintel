# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import json
from datetime import UTC, datetime
from dataclasses import asdict
from types import SimpleNamespace
from contextlib import nullcontext
from zoneinfo import ZoneInfo

import pytest

from mailintel.models import LLMPreset
from mailintel.repositories import Repository
from mailintel.services.smart_search import (
    EvidenceCompiler,
    EvidenceNode,
    SearchPlan,
    SearchTimeContext,
    SmartSearchEngine,
    SmartSearchFailure,
    SmartSearchRuntime,
)
from mailintel.settings import AppSettings


def valid_plan_payload():
    return {
        "query_intent": "lookup",
        "coverage_mode": "ranked",
        "semantic_query": "launch review deadline",
        "lexical_terms": ["launch", "review", "deadline"],
        "exact_phrases": [],
        "filters": {
            "received_from": None,
            "received_to": None,
            "subject_terms": [],
            "is_read": None,
            "has_attachments": None,
        },
        "requested_shape": "short_answer",
        "needs_clarification": False,
        "clarification_question": None,
    }


def enabled_preset(preset_id: int, *, embedding_model: str = "embed") -> LLMPreset:
    return LLMPreset(
        id=preset_id,
        name=f"preset-{preset_id}",
        enabled=True,
        embedding_model=embedding_model,
        text_context_window=8192,
        max_output_tokens=800,
    )


def preset_row(preset: LLMPreset) -> dict:
    return {
        "id": preset.id,
        "name": preset.name,
        "provider": preset.provider,
        "enabled": preset.enabled,
        "priority": preset.priority,
        "auth_mode": preset.auth_mode,
        "secret_locator": preset.secret_locator,
        "endpoint_url": preset.endpoint_url,
        "text_model": preset.text_model,
        "embedding_model": preset.embedding_model,
        "temperature": preset.temperature,
        "text_context_window": preset.text_context_window,
        "embedding_context_window": preset.embedding_context_window,
        "max_output_tokens": preset.max_output_tokens,
        "notes": preset.notes,
    }


def fixed_search_time() -> SearchTimeContext:
    return SearchTimeContext.from_utc(
        owner_timezone_name="Europe/Bratislava",
        owner_timezone=ZoneInfo("Europe/Bratislava"),
        reference_time_utc=datetime(2026, 7, 30, 12, 34, 56, tzinfo=UTC),
    )


class CapturingLLMService:
    def __init__(self, response):
        self.response = response
        self.messages = None
        self.pause_controller = SimpleNamespace(is_paused=lambda: False)

    def complete_strict_json(
        self,
        preset,
        *,
        messages,
        max_output_tokens,
        json_schema=None,
        client_id,
    ):
        self.messages = messages
        self.json_schema = json_schema
        self.max_output_tokens = max_output_tokens
        return self.response


def test_question_normalization_rejects_control_characters_and_oversize_input():
    assert SmartSearchEngine.normalize_question("  deadline?\r\n", 100) == "deadline?"
    with pytest.raises(ValueError):
        SmartSearchEngine.normalize_question("unsafe\x00query", 100)
    with pytest.raises(ValueError):
        SmartSearchEngine.normalize_question("é" * 10, 10)


def test_planner_schema_is_closed_and_date_filters_are_timezone_aware():
    payload = valid_plan_payload()
    payload["filters"]["received_from"] = "2026-07-01T00:00:00+02:00"
    plan = SmartSearchEngine._parse_plan(payload, "question")
    assert isinstance(plan, SearchPlan)
    assert plan.filters.received_from == datetime(2026, 6, 30, 22, 0, tzinfo=UTC)

    payload = valid_plan_payload()
    payload["unexpected"] = True
    with pytest.raises(SmartSearchFailure, match="invalid schema"):
        SmartSearchEngine._parse_plan(payload, "question")


def test_all_reasoning_prompts_share_one_immutable_search_reference_time():
    time_context = fixed_search_time()
    expected_context = (
        "Search reference time (treat this immutable timestamp as now for all relative dates and times):\n"
        "Owner timezone: Europe/Bratislava\n"
        "Owner local datetime: 2026-07-30T14:34:56+02:00\n"
        "UTC datetime: 2026-07-30T12:34:56+00:00"
    )
    assert time_context.prompt_text() == expected_context

    planner_llm = CapturingLLMService(valid_plan_payload())
    engine = SmartSearchEngine(AppSettings(), planner_llm)
    engine._plan(enabled_preset(1), "Where am I traveling next?", time_context)

    compression_response = {
        "relevant_claims": [{"claim": "A future trip is mentioned.", "supporting_ids": ["message:1"]}],
        "maybe_relevant": [],
        "irrelevant_input_ids": [],
        "conflicts": [],
        "limitations": [],
    }
    compressor_llm = CapturingLLMService(compression_response)
    compiler = EvidenceCompiler(
        llm_service=compressor_llm,
        compressor_preset=enabled_preset(2),
        settings=AppSettings(),
        cancelled=lambda: None,
        time_context=time_context,
    )
    compiler._compress_batch(
        "Where am I traveling next?",
        [EvidenceNode(node_id="message:1", message_ids=[1], content="Trip on 2026-08-10.")],
    )

    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "Where am I traveling next?")
    answer_messages = SmartSearchEngine._answer_messages(
        "Where am I traveling next?",
        plan,
        {"eligible_message_count": 1, "total_message_count": 1},
        [EvidenceNode(node_id="message:1", message_ids=[1], content="Trip on 2026-08-10.")],
        time_context,
    )

    assert expected_context in planner_llm.messages[1]["content"]
    assert expected_context in compressor_llm.messages[1]["content"]
    assert expected_context in answer_messages[1]["content"]


def test_answer_prompt_uses_opaque_citation_aliases_without_lineage_id_lists():
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "question")
    messages = SmartSearchEngine._answer_messages(
        "question",
        plan,
        {"eligible_message_count": 2, "total_message_count": 2},
        [
            EvidenceNode(
                node_id="compression:1",
                message_ids=[101001, 202002],
                citable_message_ids=[101001],
                content='{"kind":"query_evidence_digest","relevant_claims":["claim"]}',
            )
        ],
        fixed_search_time(),
        {101001: "e1", 202002: "e2"},
    )

    prompt = messages[1]["content"]
    assert '"represented_message_count":2' in prompt
    assert '"citable_evidence_ids":["e1"]' in prompt
    assert '"message_ids"' not in prompt
    assert '"citable_message_ids"' not in prompt
    assert "never return numeric mailbox message ids" in prompt
    assert "1-3 short sentences" in prompt
    assert "do not repeat limitations" in prompt
    schema = SmartSearchEngine._answer_json_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["answer"]["maxLength"] == 800
    assert schema["properties"]["limitations"]["maxItems"] == 2
    assert schema["properties"]["limitations"]["items"]["maxLength"] == 200
    assert schema["properties"]["supporting_evidence_ids"]["items"]["pattern"] == "^e[1-9][0-9]*$"
    assert "answer_items" in schema["required"]
    assert (
        schema["properties"]["answer_items"]["items"]["properties"]["supporting_evidence_ids"]["items"]["pattern"]
        == "^e[1-9][0-9]*$"
    )


def test_compression_batching_sizes_the_exact_serialized_prompt():
    preset = enabled_preset(2)
    settings = AppSettings(smart_search_safety_margin_tokens=0)
    compiler = EvidenceCompiler(
        llm_service=CapturingLLMService({}),
        compressor_preset=preset,
        settings=settings,
        cancelled=lambda: None,
        time_context=fixed_search_time(),
    )
    question = "Find the quoted evidence."
    nodes = [
        EvidenceNode(
            node_id=f"message:{message_id}",
            message_ids=[message_id],
            content=json.dumps(
                {"body": '"quoted" evidence with escaped JSON content ' * 20},
                separators=(",", ":"),
            ),
        )
        for message_id in (1, 2)
    ]
    output_tokens = min(max(preset.max_output_tokens, 1), 900)
    preset.text_context_window = (
        compiler.counter.count_messages(compiler._compression_messages(question, [nodes[0]]))
        + output_tokens
    )

    selected = compiler._select_batch(question, nodes)

    assert len(selected) == 1
    assert compiler._compression_fits(question, selected)
    assert not compiler._compression_fits(question, nodes)


def test_compression_batching_includes_all_nodes_that_fit_context():
    settings = AppSettings(
        smart_search_safety_margin_tokens=0,
    )
    compiler = EvidenceCompiler(
        llm_service=CapturingLLMService({}),
        compressor_preset=enabled_preset(2),
        settings=settings,
        cancelled=lambda: None,
        time_context=fixed_search_time(),
    )
    nodes = [
        EvidenceNode(node_id=f"message:{message_id}", message_ids=[message_id], content="small evidence")
        for message_id in (1, 2, 3)
    ]

    selected = compiler._select_batch("question", nodes)

    assert [node.node_id for node in selected] == ["message:1", "message:2", "message:3"]


def test_compression_batching_drains_shallowest_frontier_before_recompressing():
    settings = AppSettings(
        smart_search_safety_margin_tokens=0,
    )
    compiler = EvidenceCompiler(
        llm_service=CapturingLLMService({}),
        compressor_preset=enabled_preset(2),
        settings=settings,
        cancelled=lambda: None,
        time_context=fixed_search_time(),
    )
    nodes = [
        EvidenceNode(node_id="compression:1", message_ids=[1, 2], content="large digest " * 100, depth=1),
        EvidenceNode(node_id="message:3", message_ids=[3], content="raw evidence", depth=0),
        EvidenceNode(node_id="message:4", message_ids=[4], content="raw evidence", depth=0),
    ]

    selected = compiler._select_batch("question", nodes)

    assert [node.node_id for node in selected] == ["message:3", "message:4", "compression:1"]


def test_multinode_compression_progresses_by_reducing_the_node_frontier(monkeypatch):
    settings = AppSettings(
        smart_search_safety_margin_tokens=0,
    )
    compiler = EvidenceCompiler(
        llm_service=CapturingLLMService({}),
        compressor_preset=enabled_preset(2),
        settings=settings,
        cancelled=lambda: None,
        time_context=fixed_search_time(),
    )
    rows = [
        {"id": 1, "subject": "one", "body_text": "small evidence"},
        {"id": 2, "subject": "two", "body_text": "small evidence"},
    ]
    initial_nodes = [compiler._message_node(row) for row in rows]
    verbose_digest = "d" * 500
    assert len(verbose_digest.encode("utf-8")) > int(
        sum(len(node.content.encode("utf-8")) for node in initial_nodes) * 0.85
    )

    def compress(_question, nodes):
        compiler.call_count += 1
        return EvidenceNode(
            node_id="compression:1",
            message_ids=sorted({message_id for node in nodes for message_id in node.message_ids}),
            content=verbose_digest,
            depth=1,
            citable_message_ids=[1, 2],
        )

    monkeypatch.setattr(compiler, "_compress_batch", compress)

    def answer_messages(nodes):
        return [{"role": "user", "content": "".join(("m" * 800) + node.content for node in nodes)}]

    answer_preset = enabled_preset(3)
    answer_preset.max_output_tokens = 1
    answer_preset.text_context_window = compiler.counter.count_messages(answer_messages(initial_nodes))
    nodes, metrics = compiler.compile(
        question="question",
        rows=rows,
        answer_preset=answer_preset,
        answer_message_builder=answer_messages,
    )

    assert len(nodes) == 1
    assert nodes[0].content == verbose_digest
    assert metrics["compression_calls"] == 1


def test_single_node_recompression_must_reduce_the_exact_answer_prompt(monkeypatch):
    compiler = EvidenceCompiler(
        llm_service=CapturingLLMService({}),
        compressor_preset=enabled_preset(2),
        settings=AppSettings(smart_search_safety_margin_tokens=0),
        cancelled=lambda: None,
        time_context=fixed_search_time(),
    )
    rows = [{"id": 1, "subject": "one", "body_text": "evidence"}]
    original = compiler._message_node(rows[0])

    def compress(_question, nodes):
        compiler.call_count += 1
        return EvidenceNode(
            node_id="compression:1",
            message_ids=list(nodes[0].message_ids),
            content=original.content,
            depth=1,
            citable_message_ids=[1],
        )

    monkeypatch.setattr(compiler, "_compress_batch", compress)
    answer_preset = enabled_preset(3)
    answer_preset.max_output_tokens = 1
    answer_preset.text_context_window = 100

    with pytest.raises(SmartSearchFailure, match="single evidence node"):
        compiler.compile(
            question="question",
            rows=rows,
            answer_preset=answer_preset,
            answer_message_builder=lambda nodes: [{"role": "user", "content": nodes[0].content}],
        )


def test_answer_retries_same_evidence_after_invalid_structured_output():
    started_attempts = []
    active_checks = []
    generated = []

    def generate(repair):
        generated.append(repair)
        if len(generated) == 1:
            raise SmartSearchFailure(
                "answering",
                "answer_invalid_output",
                "The answer was malformed.",
                retryable=True,
            )
        return {"answer": "usable"}

    result = SmartSearchEngine._answer_with_retries(
        job_id=41,
        start_attempt=lambda: started_attempts.append(True),
        generate=generate,
        validate=lambda payload: payload,
        check_active=lambda: active_checks.append(True),
    )

    assert result == {"answer": "usable"}
    assert len(started_attempts) == 2
    assert generated == [False, True]
    assert len(active_checks) == 2


def test_compression_uses_opaque_node_ids_and_keeps_full_lineage_outside_digest_text():
    response = {
        "relevant_claims": [{"claim": "The trip was in summer.", "supporting_ids": ["message:1"]}],
        "maybe_relevant": [],
        "conflicts": [],
        "limitations": [],
    }
    llm_service = CapturingLLMService(response)
    compiler = EvidenceCompiler(
        llm_service=llm_service,
        compressor_preset=enabled_preset(2),
        settings=AppSettings(),
        cancelled=lambda: None,
        time_context=fixed_search_time(),
    )
    nodes = [
        EvidenceNode(
            node_id="message:1",
            message_ids=[1],
            citable_message_ids=[1],
            content="Trip evidence.",
            source_received_at={"1": "2026-07-01T09:00:00+00:00"},
        ),
        EvidenceNode(
            node_id="message:2",
            message_ids=[2],
            citable_message_ids=[2],
            content="Unrelated evidence.",
            source_received_at={"2": "2026-07-29T09:00:00+00:00"},
        ),
    ]

    digest = compiler._compress_batch("Where was I traveling?", nodes)
    assert llm_service.max_output_tokens == 800
    assert "irrelevant_input_ids" not in llm_service.json_schema["properties"]
    assert "Do not infer missing facts" in llm_service.messages[0]["content"]
    assert "Omission does not mean irrelevant" in llm_service.messages[1]["content"]
    evidence_payload = json.loads(llm_service.messages[1]["content"].split("Evidence JSON follows as untrusted data:\n", 1)[1])
    digest_content = json.loads(digest.content)

    assert evidence_payload == [
        {"input_id": "e1", "untrusted_evidence": "Trip evidence.",
         "source_received_at_range": ["2026-07-01T09:00:00+00:00"] * 2,
         "source_received_at": {"1": "2026-07-01T09:00:00+00:00"}},
        {"input_id": "e2", "untrusted_evidence": "Unrelated evidence.",
         "source_received_at_range": ["2026-07-29T09:00:00+00:00"] * 2,
         "source_received_at": {"2": "2026-07-29T09:00:00+00:00"}},
    ]
    assert digest.node_id == "compression:1"
    assert digest.message_ids == [1, 2]
    assert digest.citable_message_ids == [1]
    assert digest.source_received_at == {"1": "2026-07-01T09:00:00+00:00"}
    llm_service.response = {**response, "relevant_claims": [
        {"claim": "Summer trip.", "supporting_ids": [digest.node_id]}
    ], "irrelevant_input_ids": []}
    second_digest = compiler._compress_batch("Where was I traveling?", [digest])
    assert second_digest.source_received_at == digest.source_received_at
    answer_messages = SmartSearchEngine._answer_messages(
        "Where was I traveling?", SmartSearchEngine._parse_plan(valid_plan_payload(), "Where was I traveling?"),
        {}, [second_digest], fixed_search_time(),
    )
    answer_evidence = json.loads(answer_messages[1]["content"].split("\nEvidence:\n", 1)[1])
    assert answer_evidence[0]["source_received_at"] == {"e1": "2026-07-01T09:00:00+00:00"}
    assert digest_content == {
        "kind": "query_evidence_digest",
        "relevant_claims": ["The trip was in summer."],
        "maybe_relevant": [],
        "conflicts": [],
        "limitations": [],
    }


def test_boundaryless_complete_lookup_is_explicitly_normalized_to_ranked():
    payload = valid_plan_payload()
    payload["coverage_mode"] = "bounded_complete"
    plan = SmartSearchEngine._parse_plan(payload, "What is the airspeed velocity of an unladen swallow?")

    assert plan.coverage_mode == "ranked"
    assert plan.needs_clarification is False
    assert plan.normalizations == ["boundaryless_complete_normalized_to_ranked"]


def test_freshest_topic_lookup_uses_ranked_retrieval_without_arrival_date_clarification():
    payload = valid_plan_payload()
    payload["query_intent"] = "recent_overview"
    payload["coverage_mode"] = "bounded_complete"
    payload["semantic_query"] = "Ryanair booking information"
    payload["lexical_terms"] = ["Ryanair", "booking"]
    payload["needs_clarification"] = True
    payload["clarification_question"] = "When did that email arrive?"

    plan = SmartSearchEngine._parse_plan(payload, "give me the freshest ryanair booking info")

    assert plan.query_intent == "lookup"
    assert plan.coverage_mode == "ranked"
    assert plan.needs_clarification is False
    assert plan.filters.received_from is None
    assert plan.filters.received_to is None
    assert plan.lexical_terms == ["Ryanair", "booking"]


def test_latest_lookup_keeps_an_explicit_email_arrival_filter():
    payload = valid_plan_payload()
    payload["filters"]["received_from"] = "2026-09-01T00:00:00Z"

    plan = SmartSearchEngine._parse_plan(
        payload, "Find the latest Ryanair booking email received since September"
    )

    assert plan.coverage_mode == "ranked"
    assert plan.filters.received_from == datetime(2026, 9, 1, tzinfo=UTC)


def test_latest_exhaustive_request_still_needs_complete_coverage():
    payload = valid_plan_payload()
    payload["query_intent"] = "exhaustive_list"
    payload["coverage_mode"] = "bounded_complete"
    payload["requested_shape"] = "structured_list"
    payload["filters"]["received_from"] = "2026-09-01T00:00:00Z"

    plan = SmartSearchEngine._parse_plan(payload, "List every Ryanair booking received since September, latest first")

    assert plan.query_intent == "exhaustive_list"
    assert plan.coverage_mode == "bounded_complete"


def test_freshness_candidate_lane_is_topical_before_its_limit():
    statements = []

    def execute(sql, params):
        statements.append((sql, params))
        return []

    repository = SimpleNamespace(_execute=execute)
    rows = Repository.search_smart_search_snapshot(
        repository,
        7,
        embedding_model="qwen3-embedding-0.6b",
        query_text="Ryanair booking",
        query_vector=[0.1] * 1024,
        exact_phrases=[],
        prefer_recent=True,
        limit=40,
        statement_timeout_ms=10000,
    )

    assert rows == []
    sql, params = statements[-1]
    recent_lane = sql.split("recent_topical AS (", 1)[1].split("),\n                fused AS", 1)[0]
    assert "JOIN keyword_matches USING (id)" in recent_lane
    assert recent_lane.index("JOIN keyword_matches") < recent_lane.index("LIMIT %s")
    assert "ORDER BY received_at DESC" in recent_lane
    assert "2.0 / (60.0 + lane_rank)" in sql
    assert params[-3:] == (True, 80, 40)
    assert sql.count("%s") == len(params)


def test_planner_subject_hints_are_soft_lexical_terms_not_hard_retrieval_filters():
    payload = valid_plan_payload()
    payload["lexical_terms"] = ["summer", "travel"]
    payload["filters"]["subject_terms"] = ["flight", "hotel", "travel"]

    plan = SmartSearchEngine._parse_plan(payload, "Where was I traveling this summer?")

    assert plan.filters.subject_terms == []
    assert plan.lexical_terms == ["summer", "travel", "flight", "hotel"]
    assert plan.normalizations == ["planner_subject_terms_softened_to_lexical"]


def test_ordinary_ranked_question_does_not_block_on_planner_clarification():
    payload = valid_plan_payload()
    payload["needs_clarification"] = True
    payload["clarification_question"] = "What exact dates define this summer?"

    plan = SmartSearchEngine._parse_plan(payload, "Where was I traveling this summer?")

    assert plan.coverage_mode == "ranked"
    assert plan.needs_clarification is False
    assert plan.clarification_question is None
    assert plan.normalizations == ["ordinary_clarification_normalized_to_ranked"]
    result = SmartSearchEngine._validate_answer(
        {
            "answer": "No supplied evidence establishes a summer trip.",
            "answer_items": [],
            "supporting_message_ids": [],
            "maybe_relevant_message_ids": [],
            "evidence_sufficiency": "insufficient",
            "limitations": [],
        },
        [],
        plan,
        {"total_message_count": 1, "eligible_message_count": 1},
    )
    assert result["limitations"] == [
        "The question was ambiguous, so ranked retrieval was used and may be incomplete."
    ]


def test_ranked_coverage_normalization_is_reported_as_a_result_limitation():
    payload = valid_plan_payload()
    payload["coverage_mode"] = "bounded_complete"
    plan = SmartSearchEngine._parse_plan(payload, "find the swallow message")
    answer = {
        "answer": "One message discusses the swallow.",
        "answer_items": [
            {
                "text": "The message discusses the swallow.",
                "supporting_message_ids": [1],
                "certainty": "explicit",
            }
        ],
        "supporting_message_ids": [1],
        "maybe_relevant_message_ids": [],
        "evidence_sufficiency": "strong",
        "limitations": [],
    }

    result = SmartSearchEngine._validate_answer(
        answer,
        [{"id": 1, "body_text": "swallow"}],
        plan,
        {"total_message_count": 1, "eligible_message_count": 1},
    )

    assert result["retrieval_scope"] == "ranked"
    assert result["limitations"] == [
        "Ranked retrieval was used because complete coverage needs a date or mailbox boundary."
    ]


def test_complete_intents_without_a_boundary_request_clarification_instead_of_using_ranked_top_k():
    for intent in ("aggregate", "recent_overview", "exhaustive_list"):
        payload = valid_plan_payload()
        payload["query_intent"] = intent
        plan = SmartSearchEngine._parse_plan(payload, "what changed?")

        assert plan.needs_clarification is True
        assert "date range or explicit mailbox id" in str(plan.clarification_question)
        assert plan.normalizations == ["complete_scope_requires_boundary"]


def test_exhaustive_plan_requires_structured_list_output():
    payload = valid_plan_payload()
    payload["query_intent"] = "exhaustive_list"
    payload["coverage_mode"] = "bounded_complete"
    payload["filters"]["received_from"] = "2026-07-01T00:00:00Z"
    with pytest.raises(SmartSearchFailure, match="structured-list"):
        SmartSearchEngine._parse_plan(payload, "list every invoice from July")


def test_application_extracts_explicit_mailbox_ids_without_asking_the_planner():
    payload = valid_plan_payload()
    assert SmartSearchEngine._parse_plan(payload, "find the launch mail").filters.mailbox_ids == []

    plan = SmartSearchEngine._parse_plan(
        payload,
        "find the launch mail in mailbox 17 or mailbox id #23; mailbox 17 is preferred",
    )
    assert plan.filters.mailbox_ids == [17, 23]


def test_literal_sender_address_extraction_is_preserved_but_not_applied_as_a_filter():
    payload = valid_plan_payload()
    assert SmartSearchEngine._parse_plan(payload, "find mail from Alice").filters.sender_addresses == []

    question = "find mail from Alice <Alice+Ops@Example.TEST> or alice+ops@example.test"
    assert SmartSearchEngine._literal_sender_addresses(question) == ["alice+ops@example.test"]
    assert SmartSearchEngine._parse_plan(payload, question).filters.sender_addresses == []

    recipient_question = "find messages from Alice to bob@example.test"
    assert SmartSearchEngine._literal_sender_addresses(recipient_question) == ["bob@example.test"]
    assert SmartSearchEngine._parse_plan(payload, recipient_question).filters.sender_addresses == []


def test_planner_cannot_return_application_owned_sender_or_mailbox_filters():
    for key, value in (
        ("sender_addresses", ["alice@example.test"]),
        ("mailbox_ids", [17]),
    ):
        payload = valid_plan_payload()
        payload["filters"][key] = value
        with pytest.raises(SmartSearchFailure, match="invalid filters"):
            SmartSearchEngine._parse_plan(payload, "find mail from alice@example.test in mailbox 17")


def test_compression_validation_allows_omitted_inputs_and_rejects_unknown_ids():
    valid = {
        "relevant_claims": [{"claim": "deadline", "supporting_ids": ["message:1"]}],
        "maybe_relevant": [],
        "irrelevant_input_ids": ["message:2"],
        "conflicts": [],
        "limitations": [],
    }
    EvidenceCompiler._validate_compression(valid, ["message:1", "message:2"])

    missing = {**valid, "irrelevant_input_ids": []}
    EvidenceCompiler._validate_compression(missing, ["message:1", "message:2"])
    current = {key: value for key, value in valid.items() if key != "irrelevant_input_ids"}
    EvidenceCompiler._validate_compression(current, ["message:1", "message:2"])

    unknown = {**valid, "irrelevant_input_ids": ["message:3"]}
    with pytest.raises(SmartSearchFailure, match="unknown"):
        EvidenceCompiler._validate_compression(unknown, ["message:1", "message:2"])

    contradictory = {**valid, "irrelevant_input_ids": ["message:1", "message:2"]}
    with pytest.raises(SmartSearchFailure, match="both supported and irrelevant"):
        EvidenceCompiler._validate_compression(contradictory, ["message:1", "message:2"])


def test_compression_validation_normalizes_only_meaning_preserving_list_representations():
    payload = {
        "relevant_claims": {
            "claim": "deadline",
            "supporting_ids": "message:1",
        },
        "maybe_relevant": None,
        "irrelevant_input_ids": "message:2",
        "conflicts": None,
        "limitations": "The second message is unrelated.",
    }

    EvidenceCompiler._validate_compression(payload, ["message:1", "message:2"])

    assert payload == {
        "relevant_claims": [
            {
                "claim": "deadline",
                "supporting_ids": ["message:1"],
            }
        ],
        "maybe_relevant": [],
        "irrelevant_input_ids": ["message:2"],
        "conflicts": [],
        "limitations": ["The second message is unrelated."],
    }


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("irrelevant_input_ids", [{"input_id": "message:2"}], "irrelevant ids"),
        ("limitations", [{"text": "The second message is unrelated."}], "limitations"),
    ],
)
def test_compression_validation_identifies_invalid_scalar_list(field, value, error):
    payload = {
        "relevant_claims": [{"claim": "deadline", "supporting_ids": ["message:1"]}],
        "maybe_relevant": [],
        "irrelevant_input_ids": ["message:2"],
        "conflicts": [],
        "limitations": [],
    }
    payload[field] = value

    with pytest.raises(SmartSearchFailure, match=error):
        EvidenceCompiler._validate_compression(payload, ["message:1", "message:2"])


def test_answer_validation_fails_closed_on_invented_message_id():
    payload = {
        "answer": "The deadline is Friday.",
        "answer_items": [
            {"text": "The deadline is Friday.", "supporting_message_ids": [999], "certainty": "explicit"}
        ],
        "supporting_message_ids": [999],
        "maybe_relevant_message_ids": [],
        "evidence_sufficiency": "strong",
        "limitations": [],
    }
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "deadline?")
    with pytest.raises(SmartSearchFailure, match="not supplied"):
        SmartSearchEngine._validate_answer(
            payload,
            [{"id": 1, "body_text": "Friday"}],
            plan,
            {"total_message_count": 1, "eligible_message_count": 1},
        )


def test_answer_cannot_cite_a_message_compression_marked_non_citable():
    payload = {
        "answer": "The deadline is Friday.",
        "answer_items": [
            {"text": "The deadline is Friday.", "supporting_message_ids": [1], "certainty": "explicit"}
        ],
        "supporting_message_ids": [1],
        "maybe_relevant_message_ids": [],
        "evidence_sufficiency": "strong",
        "limitations": [],
    }
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "deadline?")
    with pytest.raises(SmartSearchFailure, match="not supplied"):
        SmartSearchEngine._validate_answer(
            payload,
            [{"id": 1, "body_text": "Friday"}],
            plan,
            {"total_message_count": 1, "eligible_message_count": 1},
            citable_message_ids=set(),
        )


def test_answer_validation_normalizes_only_meaning_preserving_list_representations():
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "swallow?")
    insufficient = {
        "answer": "No indexed message answers that question.",
        "answer_items": None,
        "supporting_message_ids": None,
        "maybe_relevant_message_ids": None,
        "evidence_sufficiency": "insufficient",
        "limitations": "No supplied evidence addressed the question.",
    }

    insufficient_result = SmartSearchEngine._validate_answer(
        insufficient,
        [],
        plan,
        {"total_message_count": 0, "eligible_message_count": 0},
    )

    assert insufficient_result["answer_items"] == []
    assert insufficient_result["supporting_message_ids"] == []
    assert insufficient_result["maybe_relevant_message_ids"] == []
    assert insufficient_result["limitations"] == ["No supplied evidence addressed the question."]

    supported = {
        "answer": "The message mentions a swallow.",
        "answer_items": {
            "text": "The message mentions a swallow.",
            "supporting_message_ids": [1],
            "certainty": "explicit",
        },
        "supporting_message_ids": [1],
        "maybe_relevant_message_ids": None,
        "evidence_sufficiency": "strong",
        "limitations": None,
    }
    supported_result = SmartSearchEngine._validate_answer(
        supported,
        [{"id": 1, "body_text": "swallow"}],
        plan,
        {"total_message_count": 1, "eligible_message_count": 1},
    )

    assert supported_result["answer_items"] == [
        {
            "text": "The message mentions a swallow.",
            "supporting_message_ids": [1],
            "certainty": "explicit",
        }
    ]
    assert supported_result["maybe_relevant_message_ids"] == []
    assert supported_result["limitations"] == []


@pytest.mark.parametrize(
    ("answer", "limitations", "error"),
    [
        ("x" * 801, [], "answer text"),
        ("No supplied evidence answers that.", ["one", "two", "three"], "limitations"),
        ("No supplied evidence answers that.", ["x" * 201], "limitations"),
    ],
)
def test_answer_validation_enforces_concise_answer_and_limitations(answer, limitations, error):
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "question")
    payload = {
        "answer": answer,
        "answer_items": [],
        "supporting_message_ids": [],
        "maybe_relevant_message_ids": [],
        "evidence_sufficiency": "insufficient",
        "limitations": limitations,
    }

    with pytest.raises(SmartSearchFailure, match=error):
        SmartSearchEngine._validate_answer(
            payload,
            [],
            plan,
            {"total_message_count": 0, "eligible_message_count": 0},
        )


def test_answer_validation_resolves_opaque_citation_aliases_to_message_sources():
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "question")
    payload = {
        "answer": "The message contains a classified marking.",
        "answer_items": [
            {
                "text": "A classified marking appears.",
                "supporting_evidence_ids": ["e1"],
                "certainty": "explicit",
            }
        ],
        "supporting_evidence_ids": ["e1"],
        "maybe_relevant_evidence_ids": [],
        "evidence_sufficiency": "strong",
        "limitations": [],
    }

    result = SmartSearchEngine._validate_answer(
        payload,
        [{"id": 101001, "subject": "Marking", "body_text": "TOP SECRET"}],
        plan,
        {"total_message_count": 1, "eligible_message_count": 1},
        citable_message_ids={101001},
        citation_aliases={"e1": 101001},
    )

    assert result["supporting_message_ids"] == [101001]
    assert result["answer_items"][0]["supporting_message_ids"] == [101001]
    assert result["sources"][0]["id"] == 101001

    payload["supporting_evidence_ids"] = ["invented"]
    with pytest.raises(SmartSearchFailure, match="not supplied"):
        SmartSearchEngine._validate_answer(
            payload,
            [{"id": 101001, "body_text": "TOP SECRET"}],
            plan,
            {"total_message_count": 1, "eligible_message_count": 1},
            citable_message_ids={101001},
            citation_aliases={"e1": 101001},
        )


def test_insufficient_answer_drops_uncited_explanatory_items():
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "swallow?")
    payload = {
        "answer": "The supplied mail evidence does not answer that question.",
        "answer_items": {
            "text": "No retrieved message provided relevant evidence.",
            "supporting_message_ids": None,
            "certainty": "uncertain",
        },
        "supporting_message_ids": None,
        "maybe_relevant_message_ids": None,
        "evidence_sufficiency": "insufficient",
        "limitations": None,
    }

    result = SmartSearchEngine._validate_answer(
        payload,
        [],
        plan,
        {"retrieved_candidate_count": 0, "total_message_count": 0, "eligible_message_count": 0},
    )

    assert result["state"] == "succeeded"
    assert result["evidence_sufficiency"] == "insufficient"
    assert result["supporting_message_ids"] == []
    assert result["sources"] == []
    assert result["answer_items"] == []


def test_insufficient_answer_may_cite_evidence_for_limited_observations():
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "does the mailbox contain classified data?")
    payload = {
        "answer": "One message has a sensitive-looking marking, but ranked evidence cannot prove mailbox-wide status.",
        "answer_items": [],
        "supporting_message_ids": [1],
        "maybe_relevant_message_ids": [],
        "evidence_sufficiency": "insufficient",
        "limitations": ["The search was ranked rather than exhaustive."],
    }

    result = SmartSearchEngine._validate_answer(
        payload,
        [{"id": 1, "subject": "Sensitive marking", "body_text": "A limited observation."}],
        plan,
        {"total_message_count": 10, "eligible_message_count": 10},
        citable_message_ids={1},
    )

    assert result["evidence_sufficiency"] == "insufficient"
    assert result["supporting_message_ids"] == [1]
    assert [(source["id"], source["search_relevance"]) for source in result["sources"]] == [
        (1, "supporting")
    ]


def test_maybe_relevant_ids_resolve_to_email_sources_without_becoming_support():
    plan = SmartSearchEngine._parse_plan(valid_plan_payload(), "where was I traveling?")
    payload = {
        "answer": "The supplied mail evidence does not confirm a destination.",
        "answer_items": [],
        "supporting_message_ids": [],
        "maybe_relevant_message_ids": [2],
        "evidence_sufficiency": "insufficient",
        "limitations": ["A flight search indicates intent, not completed travel."],
    }

    result = SmartSearchEngine._validate_answer(
        payload,
        [
            {
                "id": 2,
                "subject": "Flight search for Denver",
                "body_text": "A possible flight itinerary.",
                "sender_address": "travel@example.test",
            }
        ],
        plan,
        {"total_message_count": 1, "eligible_message_count": 1},
        citable_message_ids={2},
    )

    assert result["supporting_message_ids"] == []
    assert result["maybe_relevant_message_ids"] == [2]
    assert [(source["id"], source["search_relevance"]) for source in result["sources"]] == [
        (2, "maybe_relevant")
    ]


def test_exhaustive_answer_may_intentionally_omit_citable_messages():
    plan_payload = valid_plan_payload()
    plan_payload["query_intent"] = "exhaustive_list"
    plan_payload["coverage_mode"] = "bounded_complete"
    plan_payload["requested_shape"] = "structured_list"
    plan_payload["filters"]["received_from"] = "2026-07-01T00:00:00Z"
    plan = SmartSearchEngine._parse_plan(plan_payload, "list every invoice from July")
    payload = {
        "answer": "One invoice was found.",
        "answer_items": [
            {"text": "Invoice one", "supporting_message_ids": [1], "certainty": "explicit"}
        ],
        "supporting_message_ids": [1],
        "maybe_relevant_message_ids": [],
        "evidence_sufficiency": "strong",
        "limitations": [],
    }
    result = SmartSearchEngine._validate_answer(
        payload,
        [{"id": 1, "body_text": "invoice"}, {"id": 2, "body_text": "invoice"}],
        plan,
        {"total_message_count": 2, "eligible_message_count": 2},
        citable_message_ids={1, 2},
    )

    assert result["supporting_message_ids"] == [1]
    assert result["answer_items"] == [
        {"text": "Invoice one", "supporting_message_ids": [1], "certainty": "explicit"}
    ]


def test_structured_list_answer_requires_per_item_citations():
    plan_payload = valid_plan_payload()
    plan_payload["query_intent"] = "exhaustive_list"
    plan_payload["coverage_mode"] = "bounded_complete"
    plan_payload["requested_shape"] = "structured_list"
    plan_payload["filters"]["received_from"] = "2026-07-01T00:00:00Z"
    plan = SmartSearchEngine._parse_plan(plan_payload, "list every invoice from July")

    with pytest.raises(SmartSearchFailure, match="must contain answer items"):
        SmartSearchEngine._validate_answer(
            {
                "answer": "One invoice was found.",
                "answer_items": [],
                "supporting_message_ids": [1],
                "maybe_relevant_message_ids": [],
                "evidence_sufficiency": "strong",
                "limitations": [],
            },
            [{"id": 1, "body_text": "invoice"}],
            plan,
            {"total_message_count": 1, "eligible_message_count": 1},
            citable_message_ids={1},
        )


def test_materialization_rejects_duplicate_ids_and_oversized_complete_evidence():
    rows = [{"id": 1, "body_text": "complete body", "subject": "subject"}]
    assert SmartSearchEngine._validate_materialized_rows(rows, max_bytes=100) > 0

    with pytest.raises(SmartSearchFailure, match="duplicate"):
        SmartSearchEngine._validate_materialized_rows([*rows, *rows], max_bytes=1000)

    with pytest.raises(SmartSearchFailure, match="byte limit"):
        SmartSearchEngine._validate_materialized_rows(rows, max_bytes=5)


class SubmitRepository:
    def __init__(self):
        self.presets = {index: enabled_preset(index) for index in range(1, 5)}
        self.created = None
        self.work = {}
        self.queued = []
        for index, capability in enumerate(("search_plan", "search_embedding", "search_compress", "search_answer"), start=1):
            self.presets[index].capabilities = [capability]

    def transaction(self):
        return nullcontext()

    def _queue_lock(self):
        pass

    def _execute(self, query, params):
        if query.startswith("UPDATE smart_search_jobs SET work_json"):
            self.work = json.loads(params[0])
        return SimpleNamespace(fetchone=lambda: {"work_json": self.work})

    def get_smart_search_job(self, job_id):
        return {"status": "queued"}

    def list_llm_presets(self, enabled_only=False):
        return [dict(preset_row(preset), capabilities=preset.capabilities) for preset in self.presets.values()]

    def queue_enqueue(self, kind, key, payload, priority=1):
        self.queued.append((kind, payload, priority))

    def get_pipeline_settings(self):
        return {
            "canonical_embedding_model": "embed",
            "search_planner_preset_id": 1,
            "search_embedding_preset_id": 2,
            "search_compressor_preset_id": 3,
            "search_answer_preset_id": 4,
        }

    def get_llm_preset(self, preset_id):
        preset = self.presets.get(preset_id)
        return preset_row(preset) if preset else None

    def create_smart_search_job(self, **kwargs):
        self.created = kwargs
        return 41


def test_compression_batches_merge_latest_work_and_only_last_queues_answer(monkeypatch):
    repository = SubmitRepository()
    repository.smart_search_cancel_requested = lambda job_id: False
    repository.update_smart_search_stage = lambda *args, **kwargs: None
    remaining = {0, 1}
    original_execute = repository._execute
    def execute(query, params):
        if query.startswith("SELECT 1 FROM work_queue"):
            return SimpleNamespace(fetchone=lambda: {"pending": True} if remaining else None)
        return original_execute(query, params)
    repository._execute = execute
    engine = SmartSearchEngine(AppSettings(), SimpleNamespace())
    nodes = [EvidenceNode(node_id=f"message:{index}", message_ids=[index], citable_message_ids=[index], content="original evidence " * 20)
        for index in (1, 2)]
    repository.work = {"plan": valid_plan_payload(),
        "coverage": {}, "nodes": [asdict(node) for node in nodes], "citation_aliases": {"1": "e1", "2": "e2"},
        "reference_time": datetime.now(UTC).isoformat(), "deadline": "2099-01-01T00:00:00+00:00",
        "compression_round": 1, "compression_calls": 0, "trace": engine._new_trace()}
    repository.work["trace"]["compression"].update(batches_total=2, batches_completed=0)
    def compress(compiler, question, batch):
        return EvidenceNode(node_id=f"compression:{compiler._next_node}", message_ids=batch[0].message_ids,
            citable_message_ids=batch[0].citable_message_ids, content="short", depth=1)
    monkeypatch.setattr(EvidenceCompiler, "_compress_batch", compress)
    snapshot = json.loads(json.dumps(repository.work))
    completions = [engine._evidence_task(repository, {"job_id": 41, "question": "deadline?", "owner_timezone": "UTC"},
        snapshot, "search_compress", enabled_preset(3),
        {"round": 1, "batch": index, "node_number": index + 1, "input_node_ids": [nodes[index].node_id]})
        for index in (0, 1)]
    remaining.remove(1)
    completions[1]()
    assert repository.queued == []
    remaining.remove(0)
    completions[0]()
    assert {node["node_id"] for node in repository.work["nodes"]} == {"compression:1", "compression:2"}
    assert repository.work["compression_calls"] == 2
    assert repository.work["trace"]["compression"]["batches_completed"] == 2
    assert [kind for kind, _, _ in repository.queued] == ["search_answer"]


def test_invalid_compression_splits_once_without_exceeding_call_budget(monkeypatch):
    repository = SubmitRepository()
    repository.smart_search_cancel_requested = lambda job_id: False
    engine = SmartSearchEngine(AppSettings(smart_search_max_compression_calls=3), SimpleNamespace())
    nodes = [EvidenceNode(node_id=f"message:{i}", message_ids=[i], citable_message_ids=[i], content="evidence") for i in (1, 2)]
    repository.work = {"plan": valid_plan_payload(), "coverage": {}, "nodes": [asdict(n) for n in nodes],
        "citation_aliases": {"1": "e1", "2": "e2"}, "reference_time": datetime.now(UTC).isoformat(),
        "deadline": "2099-01-01T00:00:00+00:00", "compression_round": 1, "compression_scheduled": 1,
        "trace": {"compression": {"batches_total": 1, "batches_completed": 0}}}
    def fail(*args):
        raise SmartSearchFailure("compression", "compression_invalid_output", "invalid")
    monkeypatch.setattr(EvidenceCompiler, "_compress_batch", fail)
    job = {"job_id": 41, "question": "deadline?", "owner_timezone": "UTC"}
    payload = {"round": 1, "node_number": 1, "input_node_ids": [n.node_id for n in nodes]}
    apply = engine._evidence_task(repository, job, repository.work, "search_compress", enabled_preset(3), payload)
    apply()
    assert repository.work["compression_scheduled"] == 3
    assert repository.work["trace"]["compression"]["batches_total"] == 2
    children = [p for kind, p, priority in repository.queued]
    assert [p["input_node_ids"] for p in children] == [["message:1"], ["message:2"]]
    assert all(p["split_recovery"] for p in children)
    with pytest.raises(SmartSearchFailure):
        engine._evidence_task(repository, job, repository.work, "search_compress", enabled_preset(3), children[0])
    with pytest.raises(SmartSearchFailure):
        engine._evidence_task(repository, job, repository.work, "search_compress", enabled_preset(3), payload)


@pytest.mark.parametrize("output_limit", [128, 900, 8192])
def test_compression_keeps_existing_output_budget_and_tracks_uncited_inputs(output_limit):
    llm = CapturingLLMService({"relevant_claims": [], "maybe_relevant": [], "conflicts": [], "limitations": []})
    preset = enabled_preset(1)
    preset.max_output_tokens = output_limit
    compiler = EvidenceCompiler(llm_service=llm, compressor_preset=preset, settings=AppSettings(),
        cancelled=lambda: None, time_context=fixed_search_time())
    node = EvidenceNode(node_id="message:1", message_ids=[1], citable_message_ids=[1], content="Unrelated mail")
    result = compiler._compress_batch("When is the meeting?", [node])
    assert llm.max_output_tokens == min(output_limit, 900)
    assert result.message_ids == [1]
    assert result.citable_message_ids == []


def test_preview_terms_keep_phrases_without_domain_rules():
    terms = SmartSearchEngine._preview_terms('anything about CyberTech Europe and "annual meeting" dates')
    assert "cybertech europe" in terms
    assert "annual meeting" in terms
    assert "annual" not in terms
    assert "cybertech" not in terms
    assert "anything" not in terms
    assert "about" not in terms


def test_answer_progress_advances_before_the_provider_returns(monkeypatch):
    repository = SubmitRepository()
    repository.smart_search_cancel_requested = lambda job_id: False
    stages = []
    repository.update_smart_search_stage = lambda *args, **kwargs: stages.append(kwargs)
    engine = SmartSearchEngine(AppSettings(), SimpleNamespace())
    node = EvidenceNode(node_id="message:1", message_ids=[1], citable_message_ids=[1], content="Evidence")
    repository.work = {"plan": valid_plan_payload(), "rows": [{"id": 1}],
        "coverage": {}, "nodes": [asdict(node)], "citation_aliases": {"1": "e1"},
        "reference_time": datetime.now(UTC).isoformat(), "deadline": "2099-01-01T00:00:00+00:00",
        "trace": engine._new_trace()}
    monkeypatch.setattr(EvidenceCompiler, "_answer_fits", lambda *args: True)

    def answer(*args, **kwargs):
        assert stages[-1]["stage"] == "answering"
        assert stages[-1]["processed_count"] == 6
        assert repository.work["trace"]["compression"]["state"] == "succeeded"
        raise SmartSearchFailure("answering", "provider_test", "Provider test ended")

    monkeypatch.setattr(engine, "_answer", answer)
    with pytest.raises(SmartSearchFailure, match="Provider test ended"):
        engine._evidence_task(repository, {"job_id": 41, "question": "deadline?", "owner_timezone": "UTC"},
            repository.work, "search_answer", enabled_preset(4))


def test_runtime_routes_by_capabilities_and_queues_both_initial_stages():
    repository = SubmitRepository()
    llm_service = SimpleNamespace(pause_controller=SimpleNamespace(is_paused=lambda: False))
    runtime = SmartSearchRuntime(
        AppSettings(smart_search_owner_timezone="UTC"),
        llm_service=llm_service,
    )
    job_id = runtime.submit(repository, question="deadline?", idempotency_key="request-123")
    assert job_id == 41
    assert repository.created["planner_preset_id"] is None
    assert repository.created["embedding_preset_id"] is None
    assert repository.created["embedding_model"] == "embed"
    assert repository.created["owner_timezone"] == "UTC"
    assert repository.created["preset_fingerprints"] == {}
    assert [kind for kind, _, _ in repository.queued] == ["search_preview", "search_plan", "search_embed_query"]
    assert repository.queued[2][1]["embedding_model"] == "embed"


def test_runtime_rejects_embedding_preset_that_does_not_match_index():
    repository = SubmitRepository()
    repository.presets[2] = enabled_preset(2, embedding_model="other")
    llm_service = SimpleNamespace(pause_controller=SimpleNamespace(is_paused=lambda: False))
    runtime = SmartSearchRuntime(AppSettings(), llm_service=llm_service)
    with pytest.raises(SmartSearchFailure, match="compatible preset for search embedding"):
        runtime.submit(repository, question="deadline?", idempotency_key=None)


def test_runtime_rejects_new_jobs_without_an_answer_capability():
    repository = SubmitRepository()
    llm_service = SimpleNamespace(pause_controller=SimpleNamespace(is_paused=lambda: False))
    runtime = SmartSearchRuntime(
        AppSettings(),
        llm_service=llm_service,
    )
    repository.presets[4].capabilities = []
    with pytest.raises(SmartSearchFailure, match="compatible preset for search answer"):
        runtime.submit(repository, question="deadline?", idempotency_key=None)


def test_runtime_rejects_an_invalid_owner_timezone():
    repository = SubmitRepository()
    llm_service = SimpleNamespace(pause_controller=SimpleNamespace(is_paused=lambda: False))
    runtime = SmartSearchRuntime(
        AppSettings(smart_search_owner_timezone="Not/A_Real_Zone"),
        llm_service=llm_service,
    )
    with pytest.raises(SmartSearchFailure, match="IANA timezone"):
        runtime.submit(repository, question="deadline?", idempotency_key=None)


def test_compact_compression_rejects_only_explicitly_assessed_sources():
    llm = CapturingLLMService({'facts': [{'t': 'Conference is in October.', 's': ['e1'], 'k': 'f'}], 'drop': ['e2']})
    compiler = EvidenceCompiler(llm_service=llm, compressor_preset=enabled_preset(2),
        settings=AppSettings(), cancelled=lambda: None, time_context=fixed_search_time())
    nodes = [EvidenceNode(node_id=f'message:{i}', message_ids=[i], citable_message_ids=[i], content='email')
             for i in range(1, 4)]
    digest = compiler._compress_batch('When is the conference?', nodes)
    assert digest.message_ids == [1, 2, 3]
    assert digest.citable_message_ids == [1]
    assert digest.rejected_message_ids == [2]  # Omitted e3 is unassessed.
    llm.response = {'facts': [], 'drop': ['e1']}
    second = compiler._compress_batch('When is the conference?', [digest])
    assert second.rejected_message_ids == [1, 2]  # A digest cannot reject unseen original text.


def test_rejecting_one_segment_keeps_message_with_unassessed_or_useful_segment():
    llm = CapturingLLMService({'facts': [], 'drop': ['e1']})
    compiler = EvidenceCompiler(llm_service=llm, compressor_preset=enabled_preset(2),
        settings=AppSettings(), cancelled=lambda: None, time_context=fixed_search_time())
    nodes = [EvidenceNode(node_id=f'message:1:segment:{i}', message_ids=[1], citable_message_ids=[1], content='part')
             for i in range(2)]
    assert compiler._compress_batch('question', nodes).rejected_message_ids == []
    llm.response = {'facts': [], 'drop': ['e1', 'e2']}
    assert compiler._compress_batch('question', nodes).rejected_message_ids == [1]


@pytest.mark.parametrize('payload', [
    {'facts': [], 'drop': ['e999']},
    {'facts': [{'t': 'x'*201, 's': ['e1'], 'k': 'f'}], 'drop': []},
    {'facts': [{'t': 'fact', 's': ['e1'], 'k': 'f'}]*5, 'drop': []},
    {'facts': [{'t': 'fact', 's': ['e1'], 'k': 'f'}], 'drop': ['e1']},
])
def test_compact_compression_rejects_invalid_or_contradictory_output(payload):
    with pytest.raises(SmartSearchFailure):
        expanded = EvidenceCompiler._expand_compact_compression(payload, ['message:1'])
        EvidenceCompiler._validate_compression(expanded, ['message:1'])


def test_compression_completion_prunes_rejected_bodies_but_keeps_omissions(monkeypatch):
    repository = SubmitRepository()
    repository.smart_search_cancel_requested = lambda job_id: False
    repository.update_smart_search_stage = lambda *args, **kwargs: None
    removed = []
    repository.reject_smart_search_matches = lambda job_id, ids: removed.extend(ids)
    engine = SmartSearchEngine(AppSettings(), SimpleNamespace())
    nodes = [EvidenceNode(node_id=f'message:{i}', message_ids=[i], citable_message_ids=[i], content='body '*100)
             for i in (1, 2, 3)]
    repository.work = {'plan': valid_plan_payload(), 'coverage': {}, 'nodes': [asdict(n) for n in nodes],
        'rows': [{'id': i, 'body_text': 'email body'} for i in (1, 2, 3)],
        'citation_aliases': {'1': 'e1', '2': 'e2', '3': 'e3'}, 'reference_time': datetime.now(UTC).isoformat(),
        'deadline': '2099-01-01T00:00:00+00:00', 'compression_round': 1, 'trace': engine._new_trace()}
    monkeypatch.setattr(EvidenceCompiler, '_compress_batch', lambda *args: EvidenceNode(
        node_id='compression:1', message_ids=[1, 2, 3], citable_message_ids=[1],
        rejected_message_ids=[2], content='short', depth=1))
    complete = engine._evidence_task(repository, {'job_id': 41, 'question': 'question', 'owner_timezone': 'UTC'},
        repository.work, 'search_compress', enabled_preset(3),
        {'round': 1, 'node_number': 1, 'input_node_ids': [n.node_id for n in nodes]})
    complete()
    assert removed == [2]
    assert [row['id'] for row in repository.work['rows']] == [1, 3]
    assert repository.work['citation_aliases'] == {'1': 'e1', '3': 'e3'}
