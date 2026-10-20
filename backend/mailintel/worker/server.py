# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

import json
import sys
from typing import Any

from ..db import connect_database, init_database
from ..logging_config import configure_logging
from ..repositories import Repository
from ..settings import get_settings
from .tools import RestrictedToolRegistry


def build_tool_registry() -> RestrictedToolRegistry:
    settings = get_settings()
    connection = connect_database(settings.database_url)
    init_database(connection)
    repository = Repository(connection)
    return RestrictedToolRegistry(repository)


def handle_request(registry: RestrictedToolRegistry, payload: dict[str, Any]) -> dict[str, Any]:
    method = payload.get("method")
    if method == "list_tools":
        return {"tools": registry.list_tools()}
    if method == "call_tool":
        result = registry.call_tool(str(payload["name"]), dict(payload.get("arguments", {})))
        return {"result": result}
    raise ValueError(f"Unsupported method: {method}")


def serve_stdio() -> int:
    configure_logging()
    registry = build_tool_registry()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
            response = handle_request(registry, payload)
        except Exception as exc:  # pragma: no cover - exercised via CLI integration, not unit tests
            response = {"error": str(exc)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(serve_stdio())
