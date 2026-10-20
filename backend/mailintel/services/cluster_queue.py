# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

"""Incremental topic assignment, one provider call per label task."""
from __future__ import annotations

from dataclasses import asdict

from ..models import TopicCluster
from .llm import LLMContentValidationError
from .topics import (
    INCREMENTAL_CLUSTERING_ALGORITHM_VERSION,
    TopicClusteringService,
)


def enqueue(repository, message_id: int, canonical: str, input_hash: str) -> None:
    with repository.transaction():
        repository._queue_lock()
        repository.enqueue_incremental_clustering_message(
            message_id, canonical, INCREMENTAL_CLUSTERING_ALGORITHM_VERSION, input_hash,
        )
        repository.queue_enqueue(
            "cluster_message", f"cluster:{message_id}:{canonical}",
            {"message_id": message_id, "embedding_model": canonical, "source_hash": input_hash},
            priority=5,
        )


def discover(repository, canonical: str) -> None:
    if not canonical:
        return
    rows = list(repository._execute(
        """SELECT work.message_id, work.source_hash
           FROM message_clustering_work work
           JOIN message_embeddings me ON me.message_id = work.message_id
             AND me.embedding_model = work.embedding_model
             AND me.status = 'ready' AND me.input_hash = work.source_hash
           WHERE work.embedding_model = %s AND work.algorithm_version = %s
             AND work.status <> 'done'
             AND NOT EXISTS (SELECT 1 FROM work_queue q
                 WHERE q.dedup_key IN (
                   'cluster:' || work.message_id || ':' || work.embedding_model,
                   'label:' || work.message_id || ':' || work.embedding_model,
                   'cluster-publish:' || work.message_id || ':' || work.embedding_model))
           ORDER BY work.message_id LIMIT 25""",
        (canonical, INCREMENTAL_CLUSTERING_ALGORITHM_VERSION),
    ))
    for row in rows:
        enqueue(repository, int(row["message_id"]), canonical, str(row["source_hash"]))


def register(runtime, llm_service) -> None:
    def service(repository):
        return TopicClusteringService(repository)

    def current(repository, payload):
        row = repository.get_incremental_clustering_message(
            int(payload["message_id"]), str(payload["embedding_model"]),
        )
        return row if row and str(row.get("input_hash") or "") == payload["source_hash"] else None

    def best_cluster(repository, payload, row):
        return repository.best_incremental_topic_cluster(
            int(payload["message_id"]), payload["embedding_model"],
            embedding_text=str(row["embedding_text"]),
            dimensions=int(row["embedding_dimensions"]),
        )

    def publish(repository, payload, nearest, labelled=None):
        # The legacy row records artifact freshness only; the SQL queue owns execution.
        row = current(repository, payload)
        if row is None:
            return
        repository._execute(
            """UPDATE message_clustering_work SET status = 'in_progress', lease_expires_at = NULL
               WHERE message_id = %s AND embedding_model = %s AND source_hash = %s""",
            (payload["message_id"], payload["embedding_model"], payload["source_hash"]),
        )
        topic_service = service(repository)
        if labelled is None:
            labelled = topic_service._build_cluster_fallback(payload["embedding_model"], 0, [dict(row)])
        topic_service.process_incremental_message(
            message_id=int(payload["message_id"]), embedding_model=payload["embedding_model"],
            source_hash=payload["source_hash"], labelled=labelled, nearest=nearest,
        )

    def cluster(repository, job, preset):
        payload = job["payload"]
        row = current(repository, payload)
        if row is None:
            return None
        best = best_cluster(repository, payload, row)
        repository.rollback()
        def apply():
            if current(repository, payload) is None:
                return
            if best is not None and float(best.get("score") or 0) >= 0.78:
                publish(repository, payload, best)
            else:
                repository.queue_enqueue(
                    "label_cluster", f"label:{payload['message_id']}:{payload['embedding_model']}",
                    payload, priority=5,
                )
        return apply

    def label(repository, job, preset):
        payload = job["payload"]
        row = current(repository, payload)
        if row is None:
            return None
        # A cluster may have been published since cluster_message ran. Recheck
        # before spending a provider request on a label that will be discarded.
        best = best_cluster(repository, payload, row)
        if best is not None and float(best.get("score") or 0) >= 0.78:
            repository.rollback()
            return lambda: publish(repository, payload, best)
        topic_service = service(repository)
        fallback = topic_service._build_cluster_fallback(payload["embedding_model"], 0, [dict(row)])
        fallback.topic_key = topic_service._cluster_topic_key(
            payload["embedding_model"], 0, [dict(row)], str(row.get("embedding_text") or ""),
        )
        # Never keep a SQL transaction open while waiting for the provider.
        repository.rollback()
        labelled = llm_service.label_topic_cluster(
            preset, subjects=[str(row.get("subject") or "")],
            snippets=[str(row.get("summary_text") or row.get("cleaned_text") or "")],
            fallback=fallback, require_model_label=True,
            priority=int(job["priority"]), client_id="clustering",
        )
        if not labelled.model_name:
            raise LLMContentValidationError("Topic label requires a model response")
        def apply():
            if current(repository, payload) is not None:
                repository.queue_enqueue(
                    "cluster_publish", f"cluster-publish:{payload['message_id']}:{payload['embedding_model']}",
                    {**payload, "label": asdict(labelled)}, priority=5,
                )
        return apply

    def finish(repository, job, preset):
        payload = job["payload"]
        row = current(repository, payload)
        if row is None:
            return None
        best = best_cluster(repository, payload, row)
        repository.rollback()
        labelled = TopicCluster(**payload["label"])
        if (best is None or float(best.get("score") or 0) < 0.78) and not labelled.model_name:
            # Old queued fallback payloads must return to labeling, not create a category.
            return lambda: repository.queue_enqueue(
                "label_cluster", f"label:{payload['message_id']}:{payload['embedding_model']}",
                {key: payload[key] for key in ("message_id", "embedding_model", "source_hash")}, priority=5,
            )
        return lambda: publish(repository, payload, best, labelled)

    def relabel_topic(repository, job, preset):
        payload = job["payload"]
        topic_id = int(payload["topic_id"])
        expected_name = str(payload["expected_name"])
        topic = repository.get_topic_detail(topic_id)
        if topic is None or topic["name"] != expected_name:
            return None
        samples = repository.list_topic_label_samples(topic_id)
        if not samples:
            return None
        fallback = TopicCluster(
            id=topic_id, topic_key=f"topic:{topic_id}",
            name=expected_name, description=str(topic.get("description") or ""),
            key_terms=topic.get("key_terms_json") or [],
            importance_score=float(topic.get("importance_score") or 0),
            embedding_model=str(topic["embedding_model"]),
        )
        repository.rollback()
        labelled = llm_service.label_topic_cluster(
            preset,
            subjects=[str(row.get("subject") or "") for row in samples],
            snippets=[str(row.get("snippet") or "") for row in samples],
            fallback=fallback, require_model_label=True,
            priority=int(job["priority"]), client_id="topic_relabel",
        )
        if not labelled.model_name:
            raise LLMContentValidationError("Topic label requires a model response")
        return lambda: repository.relabel_topic_if_unchanged(
            topic_id, expected_name, labelled.name, labelled.description,
        )

    runtime.register("cluster_message", cluster, operation="clustering")
    runtime.register("label_cluster", label, operation="generation", model="text", capability="topic_label")
    runtime.register("cluster_publish", finish, operation="clustering")
    runtime.register("relabel_topic", relabel_topic, operation="generation", model="text", capability="topic_label")
