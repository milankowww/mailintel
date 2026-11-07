# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from __future__ import annotations

from fastapi import FastAPI

from ..dependencies import AppContext
from . import google_oauth, accounts, dashboard, interests, llm, maintenance, processing, profile, smart_search, static, mobile_sync


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    for module in (
        static,
        accounts,
        google_oauth,
        profile,
        llm,
        processing,
        interests,
        maintenance,
        smart_search,
        dashboard,
        mobile_sync,
    ):
        module.register_routes(app, ctx)
