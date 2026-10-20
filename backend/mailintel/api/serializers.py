# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import json


def row_to_dict(row: object) -> dict[str, object]:
    payload = dict(row)
    for field in (
        "settings_json",
        "capabilities_json",
        "collaborators_json",
        "recipients_json",
        "cc_json",
        "key_terms_json",
        "key_entities_json",
        "metadata_json",
        "summary_json",
    ):
        if field in payload and isinstance(payload[field], str):
            payload[field] = json.loads(payload[field])
    return payload


def search_activity_label(stage_counts: dict[str, int]) -> str:
    if not stage_counts:
        return ""
    labels = {
        "planning": "Planning retrieval",
        "embedding": "Embedding query",
        "reranking": "Analyzing and reranking evidence",
        "compression": "Compressing evidence",
        "answering": "Answering",
        "analysis": "Preparing search",
    }
    ordered = ["planning", "embedding", "reranking", "compression", "answering", "analysis"]
    active = [labels[stage] for stage in ordered if int(stage_counts.get(stage) or 0) > 0]
    return ", ".join(active)
