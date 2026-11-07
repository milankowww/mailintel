# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from fastapi import FastAPI, HTTPException

from ...auth import secured_route
from ..dependencies import AppContext


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    @secured_route(app, "GET", "/api/sync", capability="mail.read")
    def sync_page(after: int = 0, limit: int = 500):
        if after < 0 or after > 9223372036854775807 or not 1 <= limit <= 2000:
            raise HTTPException(status_code=400, detail="Invalid sync cursor or page size")
        page = ctx.repository.mobile_sync_page(after, limit)
        if page.pop("reset_required", False):
            raise HTTPException(status_code=409, detail="Sync cursor is ahead of the server; reset local downloads")
        return page

    @secured_route(app, "GET", "/api/sync/ids", capability="mail.read")
    def sync_ids():
        return ctx.repository.mobile_sync_ids()
