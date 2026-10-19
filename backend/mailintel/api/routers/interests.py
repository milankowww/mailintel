from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...auth import secured_route
from ...models import InterestProfile
from ..dependencies import AppContext, integer, number, optional_int, raise_name_conflict, required_text, route_payload, string_list
from ..schemas import InterestProfilePayload
from ..serializers import row_to_dict


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository

    @secured_route(app, "GET", "/api/interest-profiles", capability="mail.read")
    def list_interest_profiles():
        return {"interest_profiles": [row_to_dict(row) for row in repository.list_interest_profiles()]}

    @secured_route(app, "POST", "/api/interest-profiles", capability="settings.write")
    def save_interest_profile(payload: InterestProfilePayload):
        payload = route_payload(payload)
        try:
            profile_id = repository.upsert_interest_profile(
                InterestProfile(
                    id=optional_int(payload, "id"),
                    name=required_text(payload, "name"),
                    description=str(payload.get("description", "")),
                    collaborators=string_list(payload, "collaborators"),
                    signals=str(payload.get("signals", "")),
                    priority_weight=number(payload, "priority_weight", 1.0),
                    dashboard_prominence=integer(payload, "dashboard_prominence", 1),
                    summary_detail=str(payload.get("summary_detail", "standard")),
                    watch_mode=str(payload.get("watch_mode", "watch")),
                )
            )
        except Exception as exc:
            raise_name_conflict(exc, "interest profile")
        return {"interest_profile_id": profile_id}

    @secured_route(app, "DELETE", "/api/interest-profiles/{profile_id}", capability="settings.write")
    def delete_interest_profile(profile_id: int):
        deleted = repository.delete_interest_profile(profile_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="interest profile not found")
        return {"status": "ok", "deleted_interest_profile_id": profile_id}
