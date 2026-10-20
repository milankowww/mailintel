# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from dataclasses import asdict

import pytest

from mailintel.models import TopicCluster
from mailintel.services import cluster_queue
from mailintel.services.llm import LLMContentValidationError


class StubRuntime:
    def __init__(self):
        self.handlers = {}

    def register(self, kind, handler, **_kwargs):
        self.handlers[kind] = handler


class StubRepository:
    def __init__(self, score):
        self.score = score
        self.queued = []
        self.assigned = []

    def get_incremental_clustering_message(self, message_id, embedding_model):
        assert (message_id, embedding_model) == (42, "embed-model")
        return {"id": 42, "input_hash": "hash", "subject": "A topic",
                "summary_text": "Topic summary", "embedding_text": "[1,0]", "embedding_dimensions": 2}

    def best_incremental_topic_cluster(self, message_id, embedding_model, **kwargs):
        assert (message_id, embedding_model) == (42, "embed-model")
        assert kwargs == {"embedding_text": "[1,0]", "dimensions": 2}
        return {"id": 7, "score": self.score}

    def rollback(self):
        pass

    def _execute(self, _query, _params):
        pass

    def assign_incremental_message_to_cluster(self, **kwargs):
        self.assigned.append(kwargs)
        return 3

    def queue_enqueue(self, kind, key, payload, priority):
        self.queued.append((kind, key, payload, priority))


class FailingLLM:
    def label_topic_cluster(self, *_args, **_kwargs):
        raise AssertionError("a matching cluster should not need a label request")


def test_label_job_rechecks_cluster_before_provider_call():
    runtime = StubRuntime()
    cluster_queue.register(runtime, FailingLLM())
    repository = StubRepository(score=0.91)
    job = {"payload": {"message_id": 42, "embedding_model": "embed-model", "source_hash": "hash"},
           "priority": 5}

    apply = runtime.handlers["label_cluster"](repository, job, None)
    assert apply is not None
    apply()

    assert len(repository.assigned) == 1
    assert repository.assigned[0]["topic_cluster_id"] == 7
    assert repository.queued == []


def test_publish_job_uses_current_embedding_for_nearest_check():
    runtime = StubRuntime()
    cluster_queue.register(runtime, FailingLLM())
    repository = StubRepository(score=0.91)
    label = TopicCluster(id=None, topic_key="key", name="A topic", description="Topic",
                         key_terms=["topic"], importance_score=0.5)
    job = {"payload": {"message_id": 42, "embedding_model": "embed-model",
                       "source_hash": "hash", "label": asdict(label)}, "priority": 5}

    apply = runtime.handlers["cluster_publish"](repository, job, None)
    assert apply is not None
    apply()

    assert repository.assigned[0]["topic_cluster_id"] == 7


def test_label_job_rejects_fallback_when_no_cluster_matches():
    runtime = StubRuntime()
    cluster_queue.register(runtime, type("FallbackLLM", (), {
        "label_topic_cluster": lambda _self, _preset, **kwargs: kwargs["fallback"],
    })())
    repository = StubRepository(score=0.2)
    job = {"payload": {"message_id": 42, "embedding_model": "embed-model", "source_hash": "hash"},
           "priority": 5}

    with pytest.raises(LLMContentValidationError, match="requires a model response"):
        runtime.handlers["label_cluster"](repository, job, None)
    assert repository.queued == []


def test_old_fallback_publish_returns_to_label_queue():
    runtime = StubRuntime()
    cluster_queue.register(runtime, FailingLLM())
    repository = StubRepository(score=0.2)
    label = TopicCluster(id=None, topic_key="key", name="Fallback", description="Fallback",
                         key_terms=["fallback"], importance_score=0.5)
    job = {"payload": {"message_id": 42, "embedding_model": "embed-model",
                       "source_hash": "hash", "label": asdict(label)}, "priority": 5}

    runtime.handlers["cluster_publish"](repository, job, None)()

    assert repository.assigned == []
    assert repository.queued == [
        ("label_cluster", "label:42:embed-model",
         {"message_id": 42, "embedding_model": "embed-model", "source_hash": "hash"}, 5),
    ]


def test_relabel_topic_uses_member_samples_and_preserves_manual_rename():
    runtime = StubRuntime()

    class LabelLLM:
        def label_topic_cluster(self, _preset, **kwargs):
            assert kwargs["subjects"] == ["Incident report"]
            assert kwargs["snippets"] == ["Account access details"]
            assert kwargs["require_model_label"] is True
            assert kwargs["priority"] == 1
            return TopicCluster(
                id=5, topic_key="topic:5", name="Account Access Incident",
                description="Reports about account access.", key_terms=[],
                importance_score=0.8, model_name="test:model",
            )

    class TopicRepository:
        name = "Security Incident"

        def get_topic_detail(self, topic_id):
            assert topic_id == 5
            return {"id": 5, "name": self.name, "description": "Old description",
                    "key_terms_json": [], "importance_score": 0.8,
                    "embedding_model": "embed-model"}

        def list_topic_label_samples(self, topic_id):
            assert topic_id == 5
            return [{"subject": "Incident report", "snippet": "Account access details"}]

        def rollback(self):
            pass

        def relabel_topic_if_unchanged(self, topic_id, expected_name, name, description):
            if self.name != expected_name:
                return False
            self.name = name
            return True

    cluster_queue.register(runtime, LabelLLM())
    repository = TopicRepository()
    job = {"payload": {"topic_id": 5, "expected_name": "Security Incident"}, "priority": 1}
    apply = runtime.handlers["relabel_topic"](repository, job, None)
    repository.name = "Manual name"
    assert apply() is False
    assert repository.name == "Manual name"
    repository.name = "Security Incident"
    assert runtime.handlers["relabel_topic"](repository, job, None)() is True
    assert repository.name == "Account Access Incident"
