from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from ..dependencies import AppContext, static_file_response


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    @app.get("/")
    def index():
        bundled_index = Path(ctx.settings.static_dir) / "build" / "index.html"
        if bundled_index.is_file():
            return static_file_response(ctx.settings, "build/index.html")
        return static_file_response(ctx.settings, "index.html")

    @app.get("/assets/{asset_path:path}")
    def static_asset(asset_path: str):
        return static_file_response(ctx.settings, f"build/assets/{asset_path}")

    @app.get("/static/{asset_path:path}")
    def legacy_static_asset(asset_path: str):
        return static_file_response(ctx.settings, f"static/{asset_path}")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}
