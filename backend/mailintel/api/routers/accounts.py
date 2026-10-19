from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ...account_secrets import account_for_response
from ...audit import audit_event
from ...auth import secured_route
from ...models import MailAccount
from ..schemas import AccountPayload, MailboxUpdatePayload
from ..dependencies import (
    AppContext,
    discover_mailboxes_for_account,
    object_map,
    optional_int,
    required_text,
    route_payload,
)
from ..serializers import row_to_dict


def register_routes(app: FastAPI, ctx: AppContext) -> None:
    repository = ctx.repository
    password_encryptor = ctx.password_encryptor

    @secured_route(app, "GET", "/api/accounts", capability="mail.read")
    def list_accounts():
        accounts = []
        for row in repository.list_mail_accounts():
            acc_dict = row_to_dict(row)
            acc_dict["sync_status"] = repository.get_account_sync_status(int(acc_dict["id"]))
            accounts.append(account_for_response(acc_dict))
        return {"accounts": accounts}

    @secured_route(app, "POST", "/api/accounts", capability="settings.write", audit="account.saved")
    def save_account(payload: AccountPayload):
        payload = route_payload(payload)
        account_id = optional_int(payload, "id")
        if account_id is not None and repository.get_mail_account(account_id) is None:
            raise HTTPException(status_code=404, detail="account not found")
        settings_payload = object_map(payload, "settings")
        for key in ("password", "google_refresh_token", "google_oauth_config", "auth_mode"):
            settings_payload.pop(key, None)
        google = payload.get("auth_mode") == "google_oauth"
        if google:
            if payload.get("provider_type") != "imap":
                raise HTTPException(400, "Google OAuth requires IMAP")
            username = str(settings_payload.get("username", "")).strip()
            if not username or "@" not in username or any(ord(c) < 32 for c in username):
                raise HTTPException(400, "Enter the Google mailbox email address")
            settings_payload.update(host="imap.gmail.com", port=993, ssl=True, ignore_tls_errors=False, username=username)
            payload["secret_locator"] = None
        sync_window_days = optional_int(payload, "sync_window_days")
        if sync_window_days is not None:
            settings_payload["sync_window_days"] = int(sync_window_days)
        else:
            settings_payload.pop("sync_window_days", None)
        encrypted_password = None
        password_encryption = None
        password = "" if google else str(payload.get("password") or "")
        if password:
            encrypted_password, password_encryption = password_encryptor.encrypt_password(password)
        account = MailAccount(
            id=account_id,
            name=required_text(payload, "name"),
            provider_type=required_text(payload, "provider_type"),
            auth_mode=str(payload.get("auth_mode", "config")),
            secret_locator=str(payload["secret_locator"]) if payload.get("secret_locator") else None,
            encrypted_password=encrypted_password,
            password_encryption=password_encryption,
            settings=settings_payload,
            status="active" if str(payload.get("status", "active")) == "active" else "disabled",
        )
        account_id = repository.upsert_mail_account(account)
        audit_event("account.saved", account_id=account_id, provider=account.provider_type, password_changed=bool(password))
        return {"account_id": account_id}

    @secured_route(app, "DELETE", "/api/accounts/{account_id}", capability="settings.write", audit="account.deleted")
    def delete_account(account_id: int):
        deleted = repository.delete_mail_account(account_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="account not found")
        audit_event("account.deleted", account_id=account_id)
        return {"status": "ok", "deleted_account_id": account_id}

    @secured_route(app, "GET", "/api/mailboxes", capability="mail.read")
    def list_mailboxes(account_id: int | None = None):
        return {"mailboxes": [row_to_dict(row) for row in repository.list_mailboxes(account_id=account_id)]}

    @secured_route(app, "POST", "/api/accounts/{account_id}/discover-mailboxes", capability="settings.write")
    def discover_mailboxes(account_id: int):
        account = repository.get_mail_account(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        rows = discover_mailboxes_for_account(ctx, account)
        return {"mailboxes": [row_to_dict(row) for row in rows]}

    @secured_route(app, "PATCH", "/api/mailboxes/{mailbox_id}", capability="settings.write")
    def update_mailbox(mailbox_id: int, payload: MailboxUpdatePayload):
        payload = route_payload(payload)
        monitored = bool(payload.get("monitored", False))
        row = next((item for item in repository.list_mailboxes() if int(item["id"]) == mailbox_id), None)
        if row is None:
            raise HTTPException(status_code=404, detail="mailbox not found")
        repository.update_mailbox_monitoring(mailbox_id, monitored)
        updated = next(item for item in repository.list_mailboxes(account_id=int(row["account_id"])) if int(item["id"]) == mailbox_id)
        return {"mailbox": row_to_dict(updated)}
