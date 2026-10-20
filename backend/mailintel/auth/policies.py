# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from typing import Any

OWNER_CAPABILITIES = frozenset(
    {
        "mail.read",
        "mail.write",
        "settings.write",
        "sync.run",
        "maintenance.destroy",
        "admin.config",
    }
)


def secured_route(router: Any, method: str, path: str, *, capability: str, audit: str | None = None, **kwargs: Any):
    if capability not in OWNER_CAPABILITIES:
        raise ValueError(f"Unknown MailIntel capability: {capability}")

    def decorator(func):
        setattr(func, "mailintel_capability", capability)
        setattr(func, "mailintel_audit_event", audit)
        route_method = getattr(router, method.lower())
        return route_method(path, **kwargs)(func)

    return decorator
