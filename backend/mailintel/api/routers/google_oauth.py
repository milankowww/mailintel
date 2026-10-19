from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pathlib import Path
from pydantic import BaseModel, Field

from ...auth import secured_route
from ...connectors.base import ConnectorError
from ...google_oauth import CALLBACK_PATH, MAIL_SCOPE, load_config, save_config, token_request, validate_redirect_uri
from ..dependencies import AppContext

COOKIE = "mailintel_google_flow"


class GoogleConfigPayload(BaseModel):
    client_id: str = Field(max_length=512)
    client_secret: str = Field(default="", max_length=512)
    redirect_uri: str = Field(max_length=2048)


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    pending: dict[str, dict] = {}
    lock = threading.Lock()

    @app.get("/google-oauth-localhost.py", include_in_schema=False)
    def local_bridge():
        return FileResponse(Path(__file__).resolve().parents[2] / "resources" / "google_oauth_localhost.py",
                            filename="google_oauth_localhost.py", media_type="text/x-python")

    @secured_route(app, "GET", "/api/google-oauth/config", capability="settings.write")
    def get_config():
        config = load_config(ctx.password_encryptor)
        return {"client_id": config.get("client_id", ""), "redirect_uri": config.get("redirect_uri", ""),
                "has_client_secret": bool(config.get("client_secret"))}

    @secured_route(app, "PUT", "/api/google-oauth/config", capability="settings.write")
    def put_config(payload: GoogleConfigPayload):
        old = load_config(ctx.password_encryptor)
        client_id = payload.client_id.strip()
        secret = payload.client_secret.strip()
        if not secret and client_id == old.get("client_id"):
            secret = old.get("client_secret", "")
        if not client_id.endswith(".apps.googleusercontent.com") or not secret:
            raise HTTPException(400, "Enter the Google web client ID and client secret.")
        try:
            redirect = validate_redirect_uri(payload.redirect_uri.strip())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        save_config(ctx.password_encryptor, {"client_id": client_id, "client_secret": secret, "redirect_uri": redirect})
        with lock:
            pending.clear()
        return {"status": "ok"}

    @secured_route(app, "POST", "/api/accounts/{account_id}/google-oauth/start", capability="settings.write")
    def start(account_id: int, request: Request):
        account = ctx.repository.get_mail_account(account_id)
        if not account or account["auth_mode"] != "google_oauth":
            raise HTTPException(400, "Save a Gmail OAuth account first.")
        config = load_config(ctx.password_encryptor)
        if not config:
            raise HTTPException(400, "Open Google setup and save the OAuth client credentials first.")
        redirect = urlsplit(config["redirect_uri"])
        # Browser must return to the same origin to supply the flow-binding cookie.
        origin = request.headers.get("origin") or str(request.base_url).rstrip("/")
        if origin.rstrip("/") != f"{redirect.scheme}://{redirect.netloc}":
            raise HTTPException(400, "Open MailIntel at the same origin as the configured callback URL, then connect Google there.")
        settings = account["settings_json"]
        if isinstance(settings, str):
            settings = json.loads(settings)
        state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with lock:
            for key in list(pending):
                if pending[key]["expires"] < time.monotonic():
                    del pending[key]
            if len(pending) >= 100:
                raise HTTPException(429, "Too many pending Google connections; retry in ten minutes.")
            pending[state] = {"binding": binding, "verifier": verifier, "expires": time.monotonic() + 600,
                              "account_id": account_id, "username": settings["username"], "config": config}
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["client_id"], "redirect_uri": config["redirect_uri"], "response_type": "code",
            "scope": f"{MAIL_SCOPE} openid email", "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "login_hint": settings["username"],
        })
        response = JSONResponse({"authorization_url": url}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, binding, max_age=600, httponly=True, secure=redirect.scheme == "https",
                            samesite="lax", path=CALLBACK_PATH)
        return response

    @app.get(CALLBACK_PATH, include_in_schema=False)
    def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        request.scope["query_string"] = b""  # Keep authorization codes out of server access logs.
        with lock:
            flow = pending.get(state)
            if not flow or not hmac.compare_digest(flow["binding"], request.cookies.get(COOKIE, "")):
                raise HTTPException(400, "Invalid Google connection session. Start Connect Google again in Mail settings.")
            del pending[state]  # Every callback is single use, including failures.
        outcome = "failed"
        if flow["expires"] < time.monotonic():
            outcome = "expired"
        elif error:
            outcome = "cancelled"
        elif code and load_config(ctx.password_encryptor) == flow["config"]:
            config = flow["config"]
            try:
                tokens = token_request({"grant_type": "authorization_code", "code": code,
                                        "client_id": config["client_id"], "client_secret": config["client_secret"],
                                        "redirect_uri": config["redirect_uri"], "code_verifier": flow["verifier"]})
                if MAIL_SCOPE not in tokens.get("scope", "").split() or not tokens.get("refresh_token"):
                    outcome = "permission"
                else:
                    identity = httpx.get("https://openidconnect.googleapis.com/v1/userinfo",
                                         headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=20)
                    identity.raise_for_status()
                    profile = identity.json()
                    if not profile.get("email_verified") or profile.get("email", "").casefold() != flow["username"].casefold():
                        outcome = "wrong-account"
                    else:
                        encrypted, encryption = ctx.password_encryptor.encrypt_password(tokens["refresh_token"])
                        saved = ctx.repository.save_google_refresh_token(flow["account_id"], flow["username"], encrypted, encryption)
                        outcome = "connected" if saved else "account-changed"
            except (ConnectorError, httpx.HTTPError, ValueError):
                outcome = "failed"
        response = RedirectResponse(f"/#/mail-settings?google={outcome}", status_code=303,
                                    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.delete_cookie(COOKIE, path=CALLBACK_PATH)
        return response
