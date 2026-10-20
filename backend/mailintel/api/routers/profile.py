# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from fastapi import FastAPI

from ...auth import secured_route
from ...models import UserProfile
from ..dependencies import AppContext, route_payload
from ..schemas import UserProfilePayload
from ..serializers import row_to_dict


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository

    @secured_route(app, "GET", "/api/user-profile", capability="mail.read")
    def get_user_profile():
        row = repository.get_user_profile()
        return {"profile": row_to_dict(row) if row else None}

    @secured_route(app, "PUT", "/api/user-profile", capability="settings.write")
    def put_user_profile(payload: UserProfilePayload):
        payload = route_payload(payload)
        repository.upsert_user_profile(
            UserProfile(
                name=str(payload.get("name", "")),
                role=str(payload.get("role", "")),
                organization=str(payload.get("organization", "")),
                aliases=list(payload.get("aliases", [])),
                email_addresses=list(payload.get("email_addresses", [])),
                collaborators=list(payload.get("collaborators", [])),
                communication_preferences=str(payload.get("communication_preferences", "")),
                about=str(payload.get("about", "")),
            )
        )
        return {"status": "ok"}
