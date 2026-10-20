# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

"""One-layer topic clustering: a topic is the cluster of its member emails."""
from __future__ import annotations

import hashlib
import re
from collections import Counter

from ..models import TopicCluster
from ..repositories import Repository


INCREMENTAL_CLUSTERING_ALGORITHM_VERSION = "recent-centroid-v1"
_NEAREST_NOT_COMPUTED = object()
TOKEN_RE = re.compile(r"[^\W\d_][\w-]{2,}")
NOISE_TERMS = {
    "doctype", "html", "xmlns", "style", "https", "http", "com", "unsubscribe",
    "email", "newsletter", "message", "messages", "mail", "update", "updates",
    "team", "today", "weekly", "monthly",
}
PHRASE_STOPWORDS = NOISE_TERMS | {
    "and", "for", "from", "into", "next", "our", "re", "regarding", "the", "this", "your",
}


def _parse_vector(text: str) -> list[float]:
    return [float(part) for part in text.strip("[]").split(",") if part]


def _tokenize(text: str) -> list[str]:
    return [token for match in TOKEN_RE.finditer(text.lower())
            if (token := match.group(0)) not in NOISE_TERMS]


def _terms(rows: list[dict[str, object]]) -> list[str]:
    tokens = [token for row in rows for token in _tokenize(
        f"{row.get('subject', '')} {row.get('summary_text', '')} {row.get('cleaned_text', '')}"
    )]
    return [term for term, _count in Counter(tokens).most_common(8)]


def _name_from_content(rows: list[dict[str, object]], terms: list[str]) -> str:
    phrases: Counter[str] = Counter()
    for row in rows:
        tokens = _tokenize(f"{row.get('subject', '')} {row.get('summary_text', '')}")
        for size in (3, 2):
            for start in range(len(tokens) - size + 1):
                phrase = tokens[start:start + size]
                if phrase[0] in PHRASE_STOPWORDS or phrase[-1] in PHRASE_STOPWORDS:
                    continue
                if len(set(phrase)) == size:
                    phrases[" ".join(phrase)] += 1
    if phrases:
        phrase = max(phrases.items(), key=lambda item: (item[1], len(item[0].split()), len(item[0])))[0]
        return " ".join(token.capitalize() for token in phrase.split())
    if terms:
        return " ".join(token.capitalize() for token in terms[:2])
    return "Important Mail"


class TopicClusteringService:
    def __init__(self, repository: Repository, *_args, **_kwargs):
        self.repository = repository

    def _build_cluster_fallback(
        self, embedding_model: str, index: int, cluster_rows: list[dict[str, object]],
    ) -> TopicCluster:
        terms = _terms(cluster_rows)
        name = _name_from_content(cluster_rows, terms)
        return TopicCluster(
            id=None,
            topic_key=f"{embedding_model}:{index}:{':'.join(terms[:3]) or 'cluster'}",
            name=name,
            description=(f"Messages about {name.lower()} and related updates." if terms
                         else "Messages about important mail."),
            key_terms=terms,
            importance_score=min(1.0, 0.35 + len(cluster_rows) / 10),
            embedding_model=embedding_model,
        )

    def _cluster_topic_key(
        self, embedding_model: str, index: int,
        cluster_rows: list[dict[str, object]], centroid_text: str,
    ) -> str:
        message_ids = ",".join(str(int(row["id"])) for row in cluster_rows)
        digest = hashlib.sha256(
            f"{embedding_model}|{index}|{centroid_text}|{message_ids}".encode("utf-8")
        ).hexdigest()[:16]
        return f"{embedding_model}:cluster:{digest}"

    def process_incremental_message(
        self, *, message_id: int, embedding_model: str,
        source_hash: str | None = None,
        algorithm_version: str = INCREMENTAL_CLUSTERING_ALGORITHM_VERSION,
        similarity_threshold: float = 0.78,
        labelled: TopicCluster | None = None,
        nearest=_NEAREST_NOT_COMPUTED,
    ) -> dict[str, object]:
        row = self.repository.get_incremental_clustering_message(message_id, embedding_model)
        if row is None:
            raise RuntimeError(f"Message {message_id} is no longer eligible for clustering.")
        current_source_hash = str(row.get("input_hash") or "")
        if source_hash is not None and current_source_hash != source_hash:
            raise RuntimeError(f"Message {message_id} embedding changed before clustering started.")
        source_hash = current_source_hash

        best = nearest
        if best is _NEAREST_NOT_COMPUTED:
            best = self.repository.best_incremental_topic_cluster(message_id, embedding_model)
        best_score = float(best.get("score") or 0.0) if best is not None else 0.0
        if best is not None and best_score >= similarity_threshold:
            cluster_id = int(best["id"])
            self.repository.assign_incremental_message_to_cluster(
                message_id=message_id,
                embedding_model=embedding_model,
                algorithm_version=algorithm_version,
                source_hash=source_hash,
                topic_cluster_id=cluster_id,
                score=best_score,
            )
            return {"action": "assigned", "message_id": message_id,
                    "topic_cluster_id": cluster_id, "score": best_score}

        if labelled is None or not labelled.model_name:
            raise RuntimeError("Creating an incremental topic requires a model label.")
        cluster_rows = [dict(row)]
        centroid_text = str(row.get("embedding_text") or "")
        labelled.topic_key = self._cluster_topic_key(
            embedding_model, 0, cluster_rows, centroid_text,
        )
        cluster_id = self.repository.publish_incremental_topic_cluster(
            message_id=message_id,
            embedding_model=embedding_model,
            algorithm_version=algorithm_version,
            source_hash=source_hash,
            cluster=labelled,
            centroid=_parse_vector(centroid_text),
            received_at=row.get("received_at"),
        )
        return {"action": "created", "message_id": message_id,
                "topic_cluster_id": cluster_id, "score": 1.0}
