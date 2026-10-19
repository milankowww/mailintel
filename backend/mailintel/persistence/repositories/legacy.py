from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

try:
    from psycopg.types.json import Jsonb
except ModuleNotFoundError as exc:  # pragma: no cover - depends on local setup
    raise RuntimeError("psycopg is required for PostgreSQL storage.") from exc

from ...models import (
    AttachmentDescriptor,
    ChatSource,
    ClusterRebuildState,
    InterestMatch,
    InterestProfile,
    JobRecord,
    LLMPreset,
    MailAccount,
    Mailbox,
    MessageBundle,
    MessageSummary,
    PipelineSettings,
    PresetStageHealth,
    NormalizedMessage,
    TopicCluster,
    TopicMembership,
    UserProfile,
)
from ...services.cleaning import clean_email_text, embedding_input, safe_email_text, safe_email_value


SEARCH_TOKEN_RE = re.compile(r"[^\W_]+")
SEARCH_STOPWORDS = {
    "about",
    "after",
    "am",
    "before",
    "from",
    "how",
    "last",
    "next",
    "please",
    "that",
    "this",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


def _json(value: Any) -> Jsonb:
    return Jsonb(value)


def interval_summary_entries_text_sql(alias: str = "s") -> str:
    return (
        f"COALESCE((SELECT string_agg(entry->>'summary', ' ') "
        f"FROM jsonb_array_elements(COALESCE({alias}.summary_json->'entries', '[]'::jsonb)) AS entry "
        f"WHERE COALESCE(entry->>'summary', '') <> ''), '')"
    )


def _interval_summary_entries_text(summary_json: Any) -> str:
    if isinstance(summary_json, str):
        try:
            summary_json = json.loads(summary_json)
        except json.JSONDecodeError:
            return ""
    if not isinstance(summary_json, dict):
        return ""
    entries = summary_json.get("entries")
    if not isinstance(entries, list):
        return ""
    parts = []
    for entry in entries:
        if isinstance(entry, dict):
            text = " ".join(str(entry.get("summary") or "").split()).strip()
            if text:
                parts.append(text)
    return "\n".join(parts)


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(item):.9g}" for item in vector) + "]"


def _vector_param(vector: list[float] | str) -> str:
    if isinstance(vector, str):
        return vector
    return _vector_literal(vector)


def _date_range_clause(alias: str, from_date: date | None, to_date: date | None) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if from_date is not None:
        clauses.append(f"{alias}.received_at >= %s::date")
        params.append(from_date)
    if to_date is not None:
        clauses.append(f"{alias}.received_at < (%s::date + interval '1 day')")
        params.append(to_date)
    return (" AND " + " AND ".join(clauses), params) if clauses else ("", [])


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _processing_source_text(message: NormalizedMessage, body_text: str) -> str:
    return "\n".join(
        [
            message.subject,
            message.sender_name,
            message.sender_address,
            "\n".join(message.recipients),
            "\n".join(message.cc),
            message.importance_hint or "",
            message.snippet_hash,
            body_text,
            "1" if message.unread else "0",
            "1" if message.has_attachments else "0",
        ]
    )


def _search_like_patterns(tokens: list[str]) -> list[str]:
    patterns = []
    for token in tokens[:12]:
        if len(token) <= 2 or token in SEARCH_STOPWORDS:
            continue
        term = token[:-3] if token.endswith("ing") and len(token) > 6 else token
        patterns.append(f"%{term}%")
    return patterns


from .work_queue import WorkQueueRepository


class Repository(WorkQueueRepository):
    def __init__(self, connection):
        self.connection = connection

    def _execute(self, query: str, params: tuple[object, ...] = ()):
        return self.connection.execute(query, params)

    def commit(self) -> None:
        if not getattr(self, "_transaction_depth", 0):
            self.connection.commit()

    def _commit(self) -> None:
        self.commit()

    def rollback(self) -> None:
        self.connection.rollback()

    def embedding_input_hash(self, input_text: str) -> str:
        return _hash_text(input_text)



    def _refresh_search_document(self, message_id: int) -> None:
        self._execute(
            """
            UPDATE messages m
            SET search_document =
              setweight(to_tsvector('simple', COALESCE(m.subject, '')), 'A') ||
              setweight(to_tsvector('simple', COALESCE(m.sender_name, '') || ' ' || COALESCE(m.sender_address, '')), 'B') ||
              setweight(to_tsvector('simple', COALESCE(src.summary_text, '')), 'B') ||
              setweight(to_tsvector('simple', COALESCE(src.body_text, m.cleaned_text, '')), 'C')
            FROM (
              SELECT m2.id, s.summary_text, bc.body_text
              FROM messages m2
              LEFT JOIN message_summaries s ON s.message_id = m2.id
              LEFT JOIN message_body_cache bc ON bc.message_id = m2.id
              WHERE m2.id = %s
            ) src
            WHERE m.id = src.id
            """,
            (message_id,),
        )

    def _refresh_interval_summary_search_document(self, summary_id: int) -> None:
        entries_text = interval_summary_entries_text_sql("s")
        self._execute(
            f"""
            UPDATE mailbox_interval_summaries s
            SET search_document =
              setweight(to_tsvector('simple', COALESCE(s.summary_type, '')), 'A') ||
              setweight(to_tsvector('simple', COALESCE(b.name, '') || ' ' || COALESCE(b.provider_mailbox_id, '')), 'B') ||
              setweight(to_tsvector('simple', COALESCE(s.interval_start_date::text, '') || ' ' || COALESCE(s.period_days::text, '')), 'B') ||
              setweight(to_tsvector('simple', {entries_text}), 'C')
            FROM mailboxes b
            WHERE b.id = s.mailbox_id AND s.id = %s
            """,
            (summary_id,),
        )

    def rebuild_search_documents(self) -> dict[str, int]:
        message_rows = list(self._execute("SELECT id FROM messages ORDER BY id"))
        for row in message_rows:
            self._refresh_search_document(int(row["id"]))
        summary_rows = list(self._execute("SELECT id FROM mailbox_interval_summaries ORDER BY id"))
        for row in summary_rows:
            self._refresh_interval_summary_search_document(int(row["id"]))
        self._commit()
        return {"messages": len(message_rows), "interval_summaries": len(summary_rows)}

    def upsert_mail_account(self, account: MailAccount) -> int:
        row = self._execute(
            """
            WITH incoming (
              id, name, provider_type, auth_mode, secret_locator, encrypted_password,
              password_encryption, settings_json, status
            ) AS (
              VALUES (%s::bigint, %s, %s, %s, %s, %s, %s, %s, %s)
            ),
            updated AS (
              UPDATE mail_accounts account
              SET name = incoming.name,
                  provider_type = incoming.provider_type,
                  auth_mode = incoming.auth_mode,
                  secret_locator = incoming.secret_locator,
                  encrypted_password = CASE
                    WHEN (incoming.auth_mode <> account.auth_mode AND 'google_oauth' IN (incoming.auth_mode, account.auth_mode))
                      OR (incoming.auth_mode = 'google_oauth' AND incoming.settings_json::jsonb->>'username' IS DISTINCT FROM account.settings_json::jsonb->>'username')
                    THEN incoming.encrypted_password
                    ELSE COALESCE(incoming.encrypted_password, account.encrypted_password) END,
                  password_encryption = COALESCE(incoming.password_encryption, account.password_encryption),
                  settings_json = incoming.settings_json,
                  status = incoming.status,
                  updated_at = now()
              FROM incoming
              WHERE incoming.id IS NOT NULL AND account.id = incoming.id
              RETURNING account.id
            ),
            inserted AS (
              INSERT INTO mail_accounts (
                id, name, provider_type, auth_mode, secret_locator, encrypted_password,
                password_encryption, settings_json, status
              )
              SELECT COALESCE(id, nextval(pg_get_serial_sequence('mail_accounts', 'id')::regclass)),
                     name, provider_type, auth_mode, secret_locator, encrypted_password,
                     password_encryption, settings_json, status
              FROM incoming
              WHERE NOT EXISTS (SELECT 1 FROM updated)
              RETURNING id
            )
            SELECT id FROM updated
            UNION ALL
            SELECT id FROM inserted
            LIMIT 1
            """,
            (
                account.id,
                account.name,
                account.provider_type,
                account.auth_mode,
                account.secret_locator,
                account.encrypted_password,
                account.password_encryption,
                _json(account.settings),
                account.status,
            ),
        ).fetchone()
        self._commit()
        return int(row["id"])

    def save_google_refresh_token(self, account_id: int, username: str, encrypted: str, encryption: str) -> bool:
        row = self._execute(
            """UPDATE mail_accounts SET encrypted_password = %s, password_encryption = %s, updated_at = now()
               WHERE id = %s AND auth_mode = 'google_oauth'
                 AND settings_json::jsonb->>'username' = %s RETURNING id""",
            (encrypted, encryption, account_id, username),
        ).fetchone()
        self._commit()
        return row is not None

    def list_mail_accounts(self) -> list[dict[str, Any]]:
        return list(self._execute("SELECT * FROM mail_accounts ORDER BY id"))

    def get_account_sync_status(self, account_id: int) -> str:
        row = self._execute(
            """
            SELECT status FROM jobs 
            WHERE job_type = 'sync' AND metadata_json->>'account_id' = %s 
            ORDER BY updated_at DESC LIMIT 1
            """,
            (str(account_id),),
        ).fetchone()
        if row:
            return row["status"]
        return "idle"


    def get_mail_account(self, account_id: int) -> dict[str, Any] | None:
        return self._execute("SELECT * FROM mail_accounts WHERE id = %s", (account_id,)).fetchone()

    def delete_mail_account(self, account_id: int) -> bool:
        cursor = self._execute("DELETE FROM mail_accounts WHERE id = %s", (account_id,))
        self._commit()
        return cursor.rowcount > 0

    def reset_processed_messages_for_accounts(self, account_ids: list[int]) -> dict[str, int]:
        if not account_ids:
            return {"deleted_messages": 0, "deleted_interval_summaries": 0}
        row = self._execute(
            """
            WITH removed_interval_summaries AS (
              DELETE FROM mailbox_interval_summaries
              WHERE mailbox_id IN (
                SELECT id FROM mailboxes WHERE account_id = ANY(%s::bigint[])
              )
              RETURNING id
            ),
            deleted AS (
              DELETE FROM messages
              WHERE mailbox_id IN (
                SELECT id FROM mailboxes WHERE account_id = ANY(%s::bigint[])
              )
              RETURNING id
            ),
            reset_mailboxes AS (
              UPDATE mailboxes
              SET sync_cursor = NULL,
                  last_synced_at = NULL
              WHERE account_id = ANY(%s::bigint[])
            ),
            removed_topic_clusters AS (
              DELETE FROM topic_clusters
              RETURNING id
            )
            SELECT
              (SELECT COUNT(*) FROM deleted) AS deleted_count,
              (SELECT COUNT(*) FROM removed_interval_summaries) AS deleted_interval_summaries
            """,
            (account_ids, account_ids, account_ids),
        ).fetchone()
        self._commit()
        return {
            "deleted_messages": int(row["deleted_count"]) if row else 0,
            "deleted_interval_summaries": int(row["deleted_interval_summaries"]) if row else 0,
        }

    def delete_topic_clusters_for_model(self, embedding_model: str) -> int:
        cursor = self._execute(
            "DELETE FROM topic_clusters WHERE embedding_model = %s",
            (embedding_model,),
        )
        self._commit()
        return cursor.rowcount

    def delete_stale_topic_clusters_for_model(self, embedding_model: str, keep_topic_keys: list[str]) -> int:
        cursor = self._execute(
            """
            DELETE FROM topic_clusters
            WHERE embedding_model = %s
              AND NOT (topic_key = ANY(%s))
            """,
            (embedding_model, keep_topic_keys),
        )
        self._commit()
        return cursor.rowcount

    def upsert_mailbox(self, mailbox: Mailbox) -> int:
        row = self._execute(
            """
            INSERT INTO mailboxes (
              account_id, provider_mailbox_id, name, monitored, sync_cursor, last_synced_at,
              crawl_status, crawl_lease_expires_at, crawl_attempt_count, crawl_last_error, next_sync_at, last_crawled_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (account_id, provider_mailbox_id) DO UPDATE SET
              name = EXCLUDED.name,
              monitored = EXCLUDED.monitored,
              sync_cursor = COALESCE(EXCLUDED.sync_cursor, mailboxes.sync_cursor),
              last_synced_at = EXCLUDED.last_synced_at,
              crawl_status = COALESCE(NULLIF(EXCLUDED.crawl_status, ''), mailboxes.crawl_status),
              crawl_lease_expires_at = COALESCE(EXCLUDED.crawl_lease_expires_at, mailboxes.crawl_lease_expires_at),
              crawl_attempt_count = EXCLUDED.crawl_attempt_count,
              crawl_last_error = EXCLUDED.crawl_last_error,
              next_sync_at = COALESCE(EXCLUDED.next_sync_at, mailboxes.next_sync_at),
              last_crawled_at = COALESCE(EXCLUDED.last_crawled_at, mailboxes.last_crawled_at)
            RETURNING id
            """,
            (
                mailbox.account_id,
                mailbox.provider_mailbox_id,
                mailbox.name,
                mailbox.monitored,
                mailbox.sync_cursor,
                mailbox.last_synced_at,
                mailbox.crawl_status,
                mailbox.crawl_lease_expires_at,
                mailbox.crawl_attempt_count,
                mailbox.crawl_last_error,
                mailbox.next_sync_at,
                mailbox.last_crawled_at,
            ),
        ).fetchone()
        self._commit()
        return int(row["id"])

    def list_mailboxes(self, account_id: int | None = None, monitored_only: bool = False) -> list[dict[str, Any]]:
        predicates = []
        params: list[object] = []
        if account_id is not None:
            predicates.append("account_id = %s")
            params.append(account_id)
        if monitored_only:
            predicates.append("monitored = true")
        query = "SELECT * FROM mailboxes"
        if predicates:
            query += " WHERE " + " AND ".join(predicates)
        query += " ORDER BY account_id, name"
        return list(self._execute(query, tuple(params)))

    def update_mailbox_monitoring(self, mailbox_id: int, monitored: bool) -> None:
        self._execute("UPDATE mailboxes SET monitored = %s WHERE id = %s", (monitored, mailbox_id))
        self._commit()

    def update_mailbox_cursor(self, mailbox_id: int, cursor_value: str | None) -> None:
        self._execute("UPDATE mailboxes SET sync_cursor = %s, last_synced_at = now() WHERE id = %s", (cursor_value, mailbox_id))
        self._commit()

    def upsert_user_profile(self, profile: UserProfile) -> None:
        self._execute(
            """
            INSERT INTO user_profile (
              id, name, role, organization, aliases_json, email_addresses_json, collaborators_json, communication_preferences, about, updated_at
            ) VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (id) DO UPDATE SET
              name = EXCLUDED.name,
              role = EXCLUDED.role,
              organization = EXCLUDED.organization,
              aliases_json = EXCLUDED.aliases_json,
              email_addresses_json = EXCLUDED.email_addresses_json,
              collaborators_json = EXCLUDED.collaborators_json,
              communication_preferences = EXCLUDED.communication_preferences,
              about = EXCLUDED.about,
              updated_at = now()
            """,
            (
                profile.name,
                profile.role,
                profile.organization,
                _json(profile.aliases),
                _json(profile.email_addresses),
                _json(profile.collaborators),
                profile.communication_preferences,
                profile.about,
            ),
        )
        self._commit()

    def get_user_profile(self) -> dict[str, Any] | None:
        return self._execute("SELECT * FROM user_profile WHERE id = 1").fetchone()

    def list_llm_presets(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM llm_presets"
        if enabled_only:
            query += " WHERE enabled = true"
        query += " ORDER BY priority, id"
        return list(self._execute(query))

    def get_active_llm_preset(self) -> dict[str, Any] | None:
        return self._execute("SELECT * FROM llm_presets WHERE enabled = true ORDER BY priority, id LIMIT 1").fetchone()

    def get_llm_preset(self, preset_id: int) -> dict[str, Any] | None:
        return self._execute("SELECT * FROM llm_presets WHERE id = %s", (preset_id,)).fetchone()

    def get_pipeline_settings(self) -> dict[str, Any]:
        row = self._execute("SELECT canonical_embedding_model FROM pipeline_settings WHERE id = 1").fetchone()
        model = str(row["canonical_embedding_model"] or "") if row else ""
        if not model:
            active = self.get_active_llm_preset()
            model = str(active["embedding_model"] or "") if active else ""
        return {"canonical_embedding_model": model}

    def update_pipeline_settings(self, settings: PipelineSettings) -> dict[str, Any]:
        self._execute(
            """INSERT INTO pipeline_settings (id, canonical_embedding_model, updated_at)
            VALUES (1, %s, now()) ON CONFLICT (id) DO UPDATE SET
              canonical_embedding_model = EXCLUDED.canonical_embedding_model,
              updated_at = now()""",
            (settings.canonical_embedding_model,),
        )
        self._commit()
        return self.get_pipeline_settings()

    def upsert_llm_preset(self, preset: LLMPreset) -> tuple[int, int]:
        from ...domain.models import CAPABILITY_MODEL_FIELDS
        if not 1 <= preset.concurrency <= 20:
            raise ValueError("preset concurrency must be between 1 and 20")
        if any(capability not in CAPABILITY_MODEL_FIELDS for capability in preset.capabilities):
            raise ValueError("unknown preset capability")
        row = self._execute(
            """
            WITH incoming (
              id, name, provider, enabled, priority, auth_mode, secret_locator, endpoint_url,
              text_model, embedding_context_window, embedding_model, temperature, text_context_window, max_output_tokens, initial_timeout_seconds, streaming_timeout_seconds, notes, concurrency, capabilities_json
            ) AS (
              VALUES (%s::bigint, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::double precision, %s::double precision, %s, %s, %s)
            ),
            updated AS (
              UPDATE llm_presets preset
              SET name = incoming.name,
                  provider = incoming.provider,
                  enabled = incoming.enabled,
                  priority = incoming.priority,
                  auth_mode = incoming.auth_mode,
                  secret_locator = incoming.secret_locator,
                  endpoint_url = incoming.endpoint_url,
                  text_model = incoming.text_model,
                  embedding_context_window = incoming.embedding_context_window,
                  embedding_model = incoming.embedding_model,
                  temperature = incoming.temperature,
                  text_context_window = incoming.text_context_window,
                  max_output_tokens = incoming.max_output_tokens,
                  initial_timeout_seconds = incoming.initial_timeout_seconds,
                  streaming_timeout_seconds = incoming.streaming_timeout_seconds,
                  notes = incoming.notes,
                  concurrency = incoming.concurrency,
                  capabilities_json = incoming.capabilities_json,
                  updated_at = now()
              FROM incoming
              WHERE incoming.id IS NOT NULL AND preset.id = incoming.id
              RETURNING preset.id
            ),
            inserted AS (
              INSERT INTO llm_presets (
                id, name, provider, enabled, priority, auth_mode, secret_locator, endpoint_url,
                text_model, embedding_context_window, embedding_model, temperature, text_context_window, max_output_tokens, initial_timeout_seconds, streaming_timeout_seconds, notes, concurrency, capabilities_json
              )
              SELECT COALESCE(id, nextval(pg_get_serial_sequence('llm_presets', 'id')::regclass)),
                     name, provider, enabled, priority, auth_mode, secret_locator, endpoint_url,
                     text_model, embedding_context_window, embedding_model, temperature, text_context_window, max_output_tokens, initial_timeout_seconds, streaming_timeout_seconds, notes, concurrency, capabilities_json
              FROM incoming
              WHERE NOT EXISTS (SELECT 1 FROM updated)
              ON CONFLICT (name) DO UPDATE SET
                provider = EXCLUDED.provider,
                enabled = EXCLUDED.enabled,
                priority = EXCLUDED.priority,
                auth_mode = EXCLUDED.auth_mode,
                secret_locator = EXCLUDED.secret_locator,
                endpoint_url = EXCLUDED.endpoint_url,
                text_model = EXCLUDED.text_model,
                embedding_context_window = EXCLUDED.embedding_context_window,
                embedding_model = EXCLUDED.embedding_model,
                temperature = EXCLUDED.temperature,
                text_context_window = EXCLUDED.text_context_window,
                max_output_tokens = EXCLUDED.max_output_tokens,
                initial_timeout_seconds = EXCLUDED.initial_timeout_seconds,
                streaming_timeout_seconds = EXCLUDED.streaming_timeout_seconds,
                notes = EXCLUDED.notes,
                concurrency = EXCLUDED.concurrency,
                capabilities_json = EXCLUDED.capabilities_json,
                updated_at = now()
              RETURNING id
            )
            SELECT id FROM updated
            UNION ALL
            SELECT id FROM inserted
            LIMIT 1
            """,
            (
                preset.id,
                preset.name,
                preset.provider,
                preset.enabled,
                preset.priority,
                preset.auth_mode,
                preset.secret_locator,
                preset.endpoint_url,
                preset.text_model,
                preset.embedding_context_window,
                preset.embedding_model,
                preset.temperature,
                preset.text_context_window,
                preset.max_output_tokens,
                preset.initial_timeout_seconds,
                preset.streaming_timeout_seconds,
                preset.notes,
                preset.concurrency,
                _json(preset.capabilities),
            ),
        ).fetchone()
        self._commit()
        pipeline_settings = self.get_pipeline_settings()
        if not pipeline_settings.get("canonical_embedding_model"):
            self.update_pipeline_settings(
                PipelineSettings(canonical_embedding_model=preset.embedding_model)
            )
        return int(row["id"]), 0

    def delete_llm_preset(self, preset_id: int) -> bool:
        cursor = self._execute("DELETE FROM llm_presets WHERE id = %s", (preset_id,))
        self._commit()
        return cursor.rowcount > 0

    def delete_all_llm_presets(self) -> int:
        cursor = self._execute("DELETE FROM llm_presets")
        self._commit()
        return cursor.rowcount

    def update_llm_preset_priorities(self, priorities: list[tuple[int, int]]) -> None:
        for preset_id, priority in priorities:
            self._execute(
                "UPDATE llm_presets SET priority = %s, updated_at = now() WHERE id = %s",
                (priority, preset_id),
            )
        self._commit()

    def restore_current_message_embeddings(self, embedding_model: str) -> int:
        # Older preset saves invalidated unrelated models without changing inputs.
        # Restore only vectors still matching completed, current processing state.
        cursor = self._execute(
            """UPDATE message_embeddings me SET status = 'ready', updated_at = now()
               FROM message_processing_state ps
               WHERE me.message_id = ps.message_id AND me.embedding_model = %s
                 AND me.status = 'stale' AND me.input_hash = ps.embedding_input_hash
                 AND ps.embedding_model = me.embedding_model
                 AND ps.summary_status = 'done' AND ps.embedding_status = 'done'
                 AND NOT ps.summary_needs_refresh AND NOT ps.embedding_needs_refresh""",
            (embedding_model,),
        )
        self._commit()
        return cursor.rowcount

    def upsert_interest_profile(self, profile: InterestProfile) -> int:
        row = self._execute(
            """
            WITH incoming (
              id, name, description, collaborators_json, signals, priority_weight,
              dashboard_prominence, summary_detail, watch_mode
            ) AS (
              VALUES (%s::bigint, %s, %s, %s, %s, %s, %s, %s, %s)
            ),
            updated AS (
              UPDATE interest_profiles profile
              SET name = incoming.name,
                  description = incoming.description,
                  collaborators_json = incoming.collaborators_json,
                  signals = incoming.signals,
                  priority_weight = incoming.priority_weight,
                  dashboard_prominence = incoming.dashboard_prominence,
                  summary_detail = incoming.summary_detail,
                  watch_mode = incoming.watch_mode,
                  updated_at = now()
              FROM incoming
              WHERE incoming.id IS NOT NULL AND profile.id = incoming.id
              RETURNING profile.id
            ),
            inserted AS (
              INSERT INTO interest_profiles (
                id, name, description, collaborators_json, signals, priority_weight,
                dashboard_prominence, summary_detail, watch_mode
              )
              SELECT COALESCE(id, nextval(pg_get_serial_sequence('interest_profiles', 'id')::regclass)),
                     name, description, collaborators_json, signals, priority_weight,
                     dashboard_prominence, summary_detail, watch_mode
              FROM incoming
              WHERE NOT EXISTS (SELECT 1 FROM updated)
              ON CONFLICT (name) DO UPDATE SET
                description = EXCLUDED.description,
                collaborators_json = EXCLUDED.collaborators_json,
                signals = EXCLUDED.signals,
                priority_weight = EXCLUDED.priority_weight,
                dashboard_prominence = EXCLUDED.dashboard_prominence,
                summary_detail = EXCLUDED.summary_detail,
                watch_mode = EXCLUDED.watch_mode,
                updated_at = now()
              RETURNING id
            )
            SELECT id FROM updated
            UNION ALL
            SELECT id FROM inserted
            LIMIT 1
            """,
            (
                profile.id,
                profile.name,
                profile.description,
                _json(profile.collaborators),
                profile.signals,
                profile.priority_weight,
                profile.dashboard_prominence,
                profile.summary_detail,
                profile.watch_mode,
            ),
        ).fetchone()
        self._commit()
        return int(row["id"])

    def list_interest_profiles(self) -> list[dict[str, Any]]:
        return list(
            self._execute(
                "SELECT * FROM interest_profiles ORDER BY dashboard_prominence DESC, priority_weight DESC, name"
            )
        )

    def delete_interest_profile(self, profile_id: int) -> bool:
        cursor = self._execute("DELETE FROM interest_profiles WHERE id = %s", (profile_id,))
        self._commit()
        return cursor.rowcount > 0

    def delete_customization_settings(self) -> dict[str, int]:
        profile_cursor = self._execute("DELETE FROM interest_profiles")
        user_cursor = self._execute("DELETE FROM user_profile")
        self._commit()
        return {
            "deleted_interest_profiles": profile_cursor.rowcount,
            "deleted_user_profiles": user_cursor.rowcount,
        }

    def delete_interval_summaries(self) -> int:
        cursor = self._execute("DELETE FROM mailbox_interval_summaries")
        self._commit()
        return cursor.rowcount

    def upsert_message(
        self,
        mailbox_id: int,
        message: NormalizedMessage,
        body_text: str,
        attachments: list[AttachmentDescriptor],
    ) -> int:
        message = replace(
            message,
            **{
                field: safe_email_value(getattr(message, field))
                for field in (
                    "provider_message_id", "internet_message_id", "thread_id", "subject",
                    "sender_name", "sender_address", "recipients", "cc", "importance_hint",
                    "snippet_hash", "metadata",
                )
            },
        )
        attachments = [
            replace(
                attachment,
                provider_attachment_id=safe_email_value(attachment.provider_attachment_id),
                filename=safe_email_text(attachment.filename),
                mime_type=safe_email_text(attachment.mime_type),
                metadata=safe_email_value(attachment.metadata),
            )
            for attachment in attachments
        ]
        cleaned_body = clean_email_text(safe_email_text(body_text))
        cleaned_message = embedding_input(message.subject, message.sender_name, message.sender_address, cleaned_body)
        source_input_hash = _hash_text(_processing_source_text(message, cleaned_body))
        row = self._execute(
            """
            INSERT INTO messages (
              mailbox_id, provider_message_id, internet_message_id, thread_id, subject, sender_name,
              sender_address, recipients_json, cc_json, received_at, sent_at, is_read, has_attachments,
              importance_hint, snippet_hash, metadata_json, cleaned_text, fetched_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (mailbox_id, provider_message_id) DO UPDATE SET
              internet_message_id = EXCLUDED.internet_message_id,
              thread_id = EXCLUDED.thread_id,
              subject = EXCLUDED.subject,
              sender_name = EXCLUDED.sender_name,
              sender_address = EXCLUDED.sender_address,
              recipients_json = EXCLUDED.recipients_json,
              cc_json = EXCLUDED.cc_json,
              received_at = EXCLUDED.received_at,
              sent_at = EXCLUDED.sent_at,
              is_read = EXCLUDED.is_read,
              has_attachments = EXCLUDED.has_attachments,
              importance_hint = EXCLUDED.importance_hint,
              snippet_hash = EXCLUDED.snippet_hash,
              metadata_json = EXCLUDED.metadata_json,
              cleaned_text = EXCLUDED.cleaned_text,
              fetched_at = now()
            RETURNING id
            """,
            (
                mailbox_id,
                message.provider_message_id,
                message.internet_message_id,
                message.thread_id,
                message.subject,
                message.sender_name,
                message.sender_address,
                _json(message.recipients),
                _json(message.cc),
                message.received_at,
                message.sent_at,
                not message.unread,
                message.has_attachments,
                message.importance_hint,
                message.snippet_hash,
                _json(message.metadata),
                cleaned_message,
            ),
        ).fetchone()
        message_id = int(row["id"])
        self._execute("DELETE FROM attachments WHERE message_id = %s", (message_id,))
        for attachment in attachments:
            self._execute(
                """
                INSERT INTO attachments (message_id, provider_attachment_id, filename, mime_type, size_bytes, metadata_json)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    message_id,
                    attachment.provider_attachment_id,
                    attachment.filename,
                    attachment.mime_type,
                    attachment.size_bytes,
                    _json(attachment.metadata),
                ),
            )
        self._execute(
            """
            INSERT INTO message_processing_state (
              message_id, source_input_hash, interest_status, interest_input_hash,
              summary_status, summary_input_hash, embedding_status, embedding_input_hash, embedding_model, updated_at
            ) VALUES (%s, %s, 'pending', '', 'pending', '', 'pending', '', '', now())
            ON CONFLICT (message_id) DO UPDATE SET
              source_input_hash = EXCLUDED.source_input_hash,
              interest_status = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.interest_status
                ELSE 'pending'
              END,
              interest_input_hash = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.interest_input_hash
                ELSE ''
              END,
              summary_status = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.summary_status
                ELSE 'pending'
              END,
              summary_input_hash = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.summary_input_hash
                ELSE ''
              END,
              embedding_status = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.embedding_status
                ELSE 'pending'
              END,
              embedding_input_hash = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.embedding_input_hash
                ELSE ''
              END,
              embedding_model = CASE
                WHEN message_processing_state.source_input_hash = EXCLUDED.source_input_hash THEN message_processing_state.embedding_model
                ELSE ''
              END,
              updated_at = now()
            """,
            (message_id, source_input_hash),
        )
        self._refresh_search_document(message_id)
        self._commit()
        return message_id

    def upsert_message_body_cache(self, message_id: int, body_text: str, source_segment: str = "body") -> None:
        cleaned_body = clean_email_text(safe_email_text(body_text))
        self._execute(
            """
            INSERT INTO message_body_cache (message_id, body_text, source_segment, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (message_id) DO UPDATE SET
              body_text = EXCLUDED.body_text,
              source_segment = EXCLUDED.source_segment,
              updated_at = now()
            """,
            (message_id, cleaned_body, source_segment),
        )
        self._refresh_search_document(message_id)
        self._commit()

    def upsert_message_bundle(self, mailbox_id: int, bundle: MessageBundle) -> int:
        message_id = self.upsert_message(mailbox_id, bundle.message, bundle.body_text, bundle.attachments)
        self.upsert_message_body_cache(message_id, bundle.body_text, bundle.body_source_segment)
        return message_id

    def get_message_processing_state(self, message_id: int) -> dict[str, Any] | None:
        return self._execute(
            "SELECT * FROM message_processing_state WHERE message_id = %s",
            (message_id,),
        ).fetchone()

    def upsert_summary(self, summary: MessageSummary) -> None:
        if summary.message_id is None:
            raise ValueError("summary.message_id must be set before persistence")
        self._execute(
            """
            INSERT INTO message_summaries (
              message_id, summary_text, importance_score, rationale, confidence, model_name,
              summarized_at, created_at, updated_at
            )
            SELECT %s, %s, %s, %s, %s, %s, now(), now(), now()
            WHERE EXISTS (SELECT 1 FROM messages WHERE id = %s)
            ON CONFLICT (message_id) DO UPDATE SET
              summary_text = EXCLUDED.summary_text,
              importance_score = EXCLUDED.importance_score,
              rationale = EXCLUDED.rationale,
              confidence = EXCLUDED.confidence,
              model_name = EXCLUDED.model_name,
              summarized_at = now(),
              updated_at = now()
            """,
            (
                summary.message_id,
                summary.summary_text,
                summary.importance_score,
                summary.rationale,
                summary.confidence,
                summary.model_name,
                summary.message_id,
            ),
        )
        self._refresh_search_document(summary.message_id)
        self._commit()

    def remove_rule_based_summaries(self) -> int:
        rows = list(self._execute(
            """
            UPDATE message_summaries
            SET summary_text = '',
                rationale = 'LLM summary unavailable: previous non-LLM fallback summary was removed and will be retried on the next account sync.',
                confidence = 0.0,
                model_name = 'llm/error',
                updated_at = now()
            WHERE model_name = 'rules/v1'
            RETURNING message_id
            """,
        ))
        for row in rows:
            self._refresh_search_document(int(row["message_id"]))
        self._commit()
        return len(rows)

    def replace_interest_matches(self, message_id: int, matches: list[InterestMatch]) -> None:
        self._execute("DELETE FROM message_interest_matches WHERE message_id = %s", (message_id,))
        for match in matches:
            self._execute(
                """
                INSERT INTO message_interest_matches (message_id, interest_profile_id, score, rationale, primary_match)
                SELECT %s, %s, %s, %s, %s
                WHERE EXISTS (SELECT 1 FROM messages WHERE id = %s)
                """,
                (message_id, match.interest_profile_id, match.score, match.rationale, match.primary_match, message_id),
            )
        self._commit()

    def mark_interest_matches_done(self, message_id: int, input_text: str) -> None:
        self._execute(
            """
            UPDATE message_processing_state
            SET interest_status = 'done',
                interest_input_hash = %s,
                updated_at = now()
            WHERE message_id = %s
            """,
            (_hash_text(input_text), message_id),
        )
        self._commit()

    def mark_summary_done(self, message_id: int, input_text: str) -> None:
        self._execute(
            """
            UPDATE message_processing_state
            SET summary_status = 'done',
                summary_input_hash = %s,
                updated_at = now()
            WHERE message_id = %s
            """,
            (_hash_text(input_text), message_id),
        )
        self._commit()

    def mark_summary_pending(self, message_id: int) -> None:
        self._execute(
            """
            WITH affected AS (
              UPDATE message_processing_state
              SET summary_status = 'pending',
                  summary_input_hash = '',
                  embedding_status = CASE
                    WHEN embedding_status = 'done' THEN 'pending'
                    ELSE embedding_status
                  END,
                  embedding_needs_refresh = true,
                  updated_at = now()
              WHERE message_id = %s
              RETURNING message_id
            )
            DELETE FROM message_embeddings me
            USING affected
            WHERE me.message_id = affected.message_id
            """,
            (message_id,),
        )
        self._commit()

    def mark_analysis_refresh_pending(self) -> dict[str, int]:
        row = self._execute(
            """
            WITH affected AS (
              UPDATE message_processing_state
              SET interest_status = 'pending',
                  interest_input_hash = '',
                  interest_lease_expires_at = NULL,
                  interest_last_error = '',
                  summary_status = 'pending',
                  summary_input_hash = '',
                  summary_lease_expires_at = NULL,
                  summary_last_error = '',
                  summary_needs_refresh = true,
                  embedding_status = 'pending',
                  embedding_input_hash = '',
                  embedding_lease_expires_at = NULL,
                  embedding_last_error = '',
                  embedding_needs_refresh = true,
                  updated_at = now()
              RETURNING message_id
            ),
            deleted_matches AS (
              DELETE FROM message_interest_matches mim
              USING affected
              WHERE mim.message_id = affected.message_id
              RETURNING mim.message_id
            ),
            deleted_embeddings AS (
              DELETE FROM message_embeddings me
              USING affected
              WHERE me.message_id = affected.message_id
              RETURNING me.message_id
            )
            SELECT
              (SELECT COUNT(*) FROM affected) AS marked_messages,
              (SELECT COUNT(*) FROM deleted_matches) AS deleted_matches,
              (SELECT COUNT(*) FROM deleted_embeddings) AS deleted_embeddings
            """
        ).fetchone()
        self._commit()
        return {
            "marked_messages": int(row["marked_messages"] or 0) if row else 0,
            "deleted_matches": int(row["deleted_matches"] or 0) if row else 0,
            "deleted_embeddings": int(row["deleted_embeddings"] or 0) if row else 0,
        }

    def list_messages_for_interest_rebuild(self, limit: int = 1000) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  m.id, m.provider_message_id, b.provider_mailbox_id, m.internet_message_id, m.thread_id,
                  m.subject, m.sender_name, m.sender_address, m.recipients_json, m.cc_json,
                  m.received_at, m.sent_at, m.is_read, m.has_attachments, m.importance_hint,
                  m.snippet_hash, COALESCE(bc.body_text, m.cleaned_text, '') AS cleaned_text
                FROM messages m
                JOIN mailboxes b ON b.id = m.mailbox_id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                ORDER BY m.received_at DESC NULLS LAST, m.id DESC
                LIMIT %s
                """,
                (limit,),
            )
        )

    def upsert_topic_cluster(self, cluster: TopicCluster, *, centroid: list[float] | None = None) -> int:
        centroid_literal = _vector_literal(centroid) if centroid else None
        row = self._execute(
            """
            INSERT INTO topic_clusters (
              topic_key, embedding_model, name, description, key_terms_json,
              key_entities_json, llm_summary, why_it_matters, importance_score, centroid,
              model_name, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, now())
            ON CONFLICT (topic_key) DO UPDATE SET
              embedding_model = EXCLUDED.embedding_model,
              name = EXCLUDED.name,
              description = EXCLUDED.description,
              key_terms_json = EXCLUDED.key_terms_json,
              key_entities_json = EXCLUDED.key_entities_json,
              llm_summary = EXCLUDED.llm_summary,
              why_it_matters = EXCLUDED.why_it_matters,
              importance_score = EXCLUDED.importance_score,
              centroid = EXCLUDED.centroid,
              model_name = EXCLUDED.model_name,
              updated_at = now()
            RETURNING id
            """,
            (
                cluster.topic_key,
                cluster.embedding_model,
                cluster.name,
                cluster.description,
                _json(cluster.key_terms),
                _json(cluster.key_entities),
                cluster.llm_summary,
                cluster.why_it_matters,
                cluster.importance_score,
                centroid_literal,
                cluster.model_name,
            ),
        ).fetchone()
        self._commit()
        return int(row["id"])

    def replace_topic_memberships(self, message_id: int, memberships: list[TopicMembership]) -> None:
        self._execute("DELETE FROM message_topic_memberships WHERE message_id = %s", (message_id,))
        for membership in memberships:
            self._execute(
                """
                INSERT INTO message_topic_memberships (message_id, topic_cluster_id, score, is_primary)
                SELECT %s, %s, %s, %s
                WHERE EXISTS (SELECT 1 FROM messages WHERE id = %s)
                  AND EXISTS (SELECT 1 FROM topic_clusters WHERE id = %s)
                ON CONFLICT (message_id, topic_cluster_id) DO UPDATE SET
                  score = EXCLUDED.score,
                  is_primary = EXCLUDED.is_primary,
                  assigned_at = now()
                """,
                (
                    message_id,
                    membership.topic_cluster_id,
                    membership.score,
                    membership.is_primary,
                    message_id,
                    membership.topic_cluster_id,
                ),
            )
        self._commit()

    def list_message_stars(self) -> list[dict[str, Any]]:
        return self._execute("SELECT id, star FROM messages WHERE star > 0").fetchall()

    def set_message_star(self, message_id: int, star: int) -> dict[str, Any] | None:
        row = self._execute(
            "UPDATE messages SET star = %s WHERE id = %s RETURNING id, star",
            (star, message_id),
        ).fetchone()
        self._commit()
        return row

    def list_starred_messages(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return self._execute(
            """
            SELECT m.*, b.provider_mailbox_id,
              s.summary_text, s.importance_score, s.rationale, s.model_name,
              (s.message_id IS NOT NULL) AS has_summary,
              SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text
            FROM messages m
            JOIN mailboxes b ON b.id = m.mailbox_id
            LEFT JOIN message_summaries s ON s.message_id = m.id
            LEFT JOIN message_body_cache bc ON bc.message_id = m.id
            WHERE m.star > 0
            ORDER BY m.received_at DESC NULLS LAST, m.id DESC
            LIMIT %s OFFSET %s
            """, (limit, offset),
        ).fetchall()

    def get_message_detail(self, message_id: int) -> dict[str, Any] | None:
        return self._execute(
            """
            SELECT
              m.*,
              m.mailbox_id,
              bc.body_text,
              bc.source_segment AS body_source_segment,
              SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
              s.summary_text,
              s.importance_score,
              s.rationale,
              s.confidence,
              s.model_name,
              b.account_id,
              b.provider_mailbox_id
            FROM messages m
            JOIN mailboxes b ON b.id = m.mailbox_id
            LEFT JOIN message_body_cache bc ON bc.message_id = m.id
            LEFT JOIN message_summaries s ON s.message_id = m.id
            WHERE m.id = %s
            """,
            (message_id,),
        ).fetchone()

    def get_message_by_provider_id(self, mailbox_id: int, provider_message_id: str) -> dict[str, Any] | None:
        return self._execute(
            "SELECT * FROM messages WHERE mailbox_id = %s AND provider_message_id = %s",
            (mailbox_id, provider_message_id),
        ).fetchone()

    def message_missing_received_at(self, mailbox_id: int, provider_message_id: str) -> bool:
        row = self._execute(
            """
            SELECT received_at IS NULL AS missing_received_at
            FROM messages
            WHERE mailbox_id = %s AND provider_message_id = %s
            """,
            (mailbox_id, provider_message_id),
        ).fetchone()
        return bool(row and row["missing_received_at"])

    def message_processing_complete(
        self,
        *,
        mailbox_id: int,
        provider_message_id: str,
        embedding_model: str | None,
    ) -> bool:
        row = self._execute(
            """
            SELECT
              ps.interest_status,
              ps.summary_status,
              ps.embedding_status,
              ps.embedding_model
            FROM messages m
            JOIN message_processing_state ps ON ps.message_id = m.id
            WHERE m.mailbox_id = %s AND m.provider_message_id = %s
            """,
            (mailbox_id, provider_message_id),
        ).fetchone()
        if row is None:
            return False
        if str(row["interest_status"]) != "done":
            return False
        if str(row["summary_status"]) != "done":
            return False
        if not embedding_model:
            return True
        return str(row["embedding_status"]) == "done" and str(row["embedding_model"]) == embedding_model

    def list_messages_needing_llm_summary(self, mailbox_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT m.id, m.provider_message_id, m.subject
                FROM messages m
                LEFT JOIN message_summaries s ON s.message_id = m.id
                WHERE m.mailbox_id = %s
                  AND (s.message_id IS NULL OR s.model_name IN ('llm/error', 'rules/v1'))
                ORDER BY m.received_at DESC NULLS LAST, m.id DESC
                LIMIT %s
                """,
                (mailbox_id, limit),
            )
        )

    def list_recent_triage(
        self,
        limit: int = 50,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        date_clause, date_params = _date_range_clause("m", from_date, to_date)
        return list(
            self._execute(
                f"""
                WITH triage_rows AS (
                  SELECT
                    m.id, m.subject, m.sender_name, m.sender_address, m.received_at, m.is_read,
                    ma.name AS account_name, b.name AS mailbox_name, b.provider_mailbox_id,
                    COALESCE(s.importance_score, 0.0) AS base_importance_score,
                    CASE
                      WHEN m.received_at IS NULL THEN 0.0
                      WHEN ((current_timestamp AT TIME ZONE 'UTC')::date - (m.received_at AT TIME ZONE 'UTC')::date) = 0 THEN 0.3
                      WHEN ((current_timestamp AT TIME ZONE 'UTC')::date - (m.received_at AT TIME ZONE 'UTC')::date) BETWEEN 1 AND 2 THEN 0.2
                      WHEN ((current_timestamp AT TIME ZONE 'UTC')::date - (m.received_at AT TIME ZONE 'UTC')::date) BETWEEN 3 AND 5 THEN 0.1
                      ELSE 0.0
                    END AS recency_boost,
                    COALESCE(s.summary_text, '') AS summary_text,
                    SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                    CASE WHEN s.message_id IS NULL THEN false ELSE true END AS has_summary,
                    COALESCE(s.rationale, '') AS rationale,
                    COALESCE(s.model_name, '') AS model_name
                  FROM messages m
                  JOIN mailboxes b ON b.id = m.mailbox_id
                  JOIN mail_accounts ma ON ma.id = b.account_id
                  LEFT JOIN message_summaries s ON s.message_id = m.id
                  LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                  WHERE true{date_clause}
                ),
                ranked_rows AS (
                  SELECT *
                  FROM triage_rows
                  ORDER BY base_importance_score + recency_boost DESC, received_at DESC NULLS LAST
                  LIMIT %s
                )
                SELECT
                  ranked_rows.*,
                  base_importance_score + recency_boost AS importance_score,
                  EXISTS (
                    SELECT 1
                    FROM message_embeddings me
                    WHERE me.message_id = ranked_rows.id AND me.status = 'ready'
                  ) AS has_embeddings,
                  COALESCE(profile_matches.interest_profiles, '[]'::jsonb) AS interest_profiles
                FROM ranked_rows
                LEFT JOIN LATERAL (
                    SELECT jsonb_agg(
                      jsonb_build_object(
                        'id', profile.id,
                        'name', profile.name,
                        'score', mim.score,
                        'primary_match', mim.primary_match
                      )
                      ORDER BY mim.primary_match DESC, mim.score DESC, profile.name
                    ) AS interest_profiles
                    FROM message_interest_matches mim
                    JOIN interest_profiles profile ON profile.id = mim.interest_profile_id
                  WHERE mim.message_id = ranked_rows.id
                ) profile_matches ON true
                ORDER BY importance_score DESC, received_at DESC NULLS LAST
                """,
                (*date_params, limit),
            )
        )

    def list_messages_for_interest_profile(
        self,
        interest_profile_id: int,
        limit: int = 10,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        date_clause, date_params = _date_range_clause("m", from_date, to_date)
        return list(
            self._execute(
                f"""
                SELECT
                  m.id, m.subject, m.sender_name, m.sender_address, m.received_at, m.is_read,
                  ma.name AS account_name, b.name AS mailbox_name, b.provider_mailbox_id,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  CASE WHEN s.message_id IS NULL THEN false ELSE true END AS has_summary,
                  COALESCE(s.rationale, '') AS rationale,
                  COALESCE(s.model_name, '') AS model_name,
                  mim.score AS profile_match_score,
                  mim.rationale AS profile_match_rationale,
                  mim.primary_match,
                  CASE WHEN me.message_id IS NOT NULL THEN true ELSE false END AS has_embeddings
                FROM message_interest_matches mim
                JOIN messages m ON m.id = mim.message_id
                JOIN mailboxes b ON b.id = m.mailbox_id
                JOIN mail_accounts ma ON ma.id = b.account_id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_embeddings me ON me.message_id = m.id AND me.status = 'ready'
                WHERE mim.interest_profile_id = %s{date_clause}
                ORDER BY mim.primary_match DESC, mim.score DESC, importance_score DESC, m.received_at DESC NULLS LAST
                LIMIT %s
                """,
                (interest_profile_id, *date_params, limit),
            )
        )

    def search_messages(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        tokens = SEARCH_TOKEN_RE.findall(query.lower())
        search_text = " ".join(tokens[:12]) if tokens else query
        rows = list(
            self._execute(
                """
                SELECT
                  m.id, m.subject,
                  m.received_at,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  COALESCE(interest.max_interest_score, 0.0) AS max_interest_score,
                  COALESCE(interest.primary_interest_match, false) AS primary_interest_match,
                  ts_rank_cd(m.search_document, websearch_to_tsquery('simple', %s)) AS rank
                FROM messages m
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN LATERAL (
                  SELECT
                    MAX(mim.score) AS max_interest_score,
                    BOOL_OR(mim.primary_match) AS primary_interest_match
                  FROM message_interest_matches mim
                  WHERE mim.message_id = m.id
                ) interest ON true
                WHERE m.search_document @@ websearch_to_tsquery('simple', %s)
                ORDER BY rank DESC, m.received_at DESC NULLS LAST
                LIMIT %s
                """,
                (search_text, search_text, limit),
            )
        )
        if rows or not tokens:
            return rows
        patterns = _search_like_patterns(tokens)
        if not patterns:
            return []
        haystack = "lower(m.subject || ' ' || COALESCE(s.summary_text, '') || ' ' || COALESCE(bc.body_text, m.cleaned_text, ''))"
        predicates = " OR ".join([f"{haystack} LIKE %s"] * len(patterns))
        return list(
            self._execute(
                f"""
                SELECT
                  m.id, m.subject,
                  m.received_at,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  COALESCE(interest.max_interest_score, 0.0) AS max_interest_score,
                  COALESCE(interest.primary_interest_match, false) AS primary_interest_match,
                  0.0 AS rank
                FROM messages m
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN LATERAL (
                  SELECT
                    MAX(mim.score) AS max_interest_score,
                    BOOL_OR(mim.primary_match) AS primary_interest_match
                  FROM message_interest_matches mim
                  WHERE mim.message_id = m.id
                ) interest ON true
                WHERE {predicates}
                ORDER BY m.received_at DESC NULLS LAST
                LIMIT %s
                """,
                (*patterns, limit),
            )
        )

    def search_messages_by_embedding(self, vector: list[float], embedding_model: str, limit: int = 10) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  m.id, m.subject,
                  m.received_at,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  COALESCE(interest.max_interest_score, 0.0) AS max_interest_score,
                  COALESCE(interest.primary_interest_match, false) AS primary_interest_match,
                  (me.embedding <=> %s::vector) AS distance
                FROM message_embeddings me
                JOIN messages m ON m.id = me.message_id
                JOIN message_processing_state ps ON ps.message_id = me.message_id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN LATERAL (
                  SELECT
                    MAX(mim.score) AS max_interest_score,
                    BOOL_OR(mim.primary_match) AS primary_interest_match
                  FROM message_interest_matches mim
                  WHERE mim.message_id = m.id
                ) interest ON true
                WHERE me.embedding_model = %s
                  AND me.status = 'ready'
                  AND ps.summary_status = 'done'
                  AND ps.embedding_status = 'done'
                  AND ps.embedding_model = me.embedding_model
                  AND me.dimensions = %s
                ORDER BY me.embedding <=> %s::vector
                LIMIT %s
                """,
                (_vector_literal(vector), embedding_model, len(vector), _vector_literal(vector), limit),
            )
        )

    def search_interval_summaries(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        tokens = SEARCH_TOKEN_RE.findall(query.lower())
        search_text = " ".join(tokens[:12]) if tokens else query
        entries_text = interval_summary_entries_text_sql("s")
        rows = list(
            self._execute(
                f"""
                SELECT
                  s.id,
                  s.summary_type,
                  s.interval_start_date,
                  s.period_days,
                  s.summary_json,
                  b.name AS mailbox_name,
                  ts_rank_cd(s.search_document, websearch_to_tsquery('simple', %s)) AS rank
                FROM mailbox_interval_summaries s
                JOIN mailboxes b ON b.id = s.mailbox_id
                WHERE s.refreshed_at IS NOT NULL
                  AND {entries_text} <> ''
                  AND s.search_document @@ websearch_to_tsquery('simple', %s)
                ORDER BY rank DESC, s.interval_start_date DESC
                LIMIT %s
                """,
                (search_text, search_text, limit),
            )
        )
        if rows or not tokens:
            return rows
        patterns = _search_like_patterns(tokens)
        if not patterns:
            return []
        haystack = f"lower(s.summary_type || ' ' || s.interval_start_date::text || ' ' || b.name || ' ' || {entries_text})"
        predicates = " OR ".join([f"{haystack} LIKE %s"] * len(patterns))
        return list(
            self._execute(
                f"""
                SELECT
                  s.id,
                  s.summary_type,
                  s.interval_start_date,
                  s.period_days,
                  s.summary_json,
                  b.name AS mailbox_name,
                  0.0 AS rank
                FROM mailbox_interval_summaries s
                JOIN mailboxes b ON b.id = s.mailbox_id
                WHERE s.refreshed_at IS NOT NULL
                  AND {entries_text} <> ''
                  AND ({predicates})
                ORDER BY s.interval_start_date DESC
                LIMIT %s
                """,
                (*patterns, limit),
            )
        )

    def search_interval_summaries_by_embedding(
        self, vector: list[float], embedding_model: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        entries_text = interval_summary_entries_text_sql("s")
        return list(
            self._execute(
                f"""
                SELECT
                  s.id,
                  s.summary_type,
                  s.interval_start_date,
                  s.period_days,
                  s.summary_json,
                  b.name AS mailbox_name,
                  (ise.embedding <=> %s::vector) AS distance
                FROM interval_summary_embeddings ise
                JOIN mailbox_interval_summaries s ON s.id = ise.summary_id
                JOIN mailboxes b ON b.id = s.mailbox_id
                WHERE ise.embedding_model = %s
                  AND ise.status = 'ready'
                  AND s.embedding_status = 'done'
                  AND s.embedding_model = ise.embedding_model
                  AND ise.dimensions = %s
                  AND s.refreshed_at IS NOT NULL
                  AND {entries_text} <> ''
                ORDER BY ise.embedding <=> %s::vector
                LIMIT %s
                """,
                (_vector_literal(vector), embedding_model, len(vector), _vector_literal(vector), limit),
            )
        )

    def search_messages_hybrid(self, query: str, vector: list[float] | None, embedding_model: str | None, limit: int = 25) -> list[dict[str, Any]]:
        fts_rows = self.search_messages(query, limit=limit)
        vector_rows = []
        if vector and embedding_model:
            vector_rows = self.search_messages_by_embedding(vector, embedding_model, limit=limit)
        
        seen_ids = set()
        merged = []
        for row in fts_rows:
            row_id = int(row["id"])
            if row_id not in seen_ids:
                merged.append(row)
                seen_ids.add(row_id)
        for row in vector_rows:
            row_id = int(row["id"])
            if row_id not in seen_ids:
                merged.append(row)
                seen_ids.add(row_id)
        return merged[:limit*2]

    def get_search_context(self, message_ids: list[int]) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        return list(
            self._execute(
                """
                SELECT 
                  m.id, m.subject, m.sender_name, m.sender_address, m.received_at,
                  m.recipients_json, m.cc_json, b.name AS mailbox_name,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  COALESCE(bc.body_text, m.cleaned_text, '') AS body_text,
                  ARRAY_AGG(p.name) FILTER (WHERE p.name IS NOT NULL) as tags
                FROM messages m
                JOIN mailboxes b ON b.id = m.mailbox_id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN message_interest_matches mim ON mim.message_id = m.id
                LEFT JOIN interest_profiles p ON p.id = mim.interest_profile_id
                WHERE m.id = ANY(%s)
                GROUP BY m.id, b.name, s.summary_text, bc.body_text
                ORDER BY array_position(%s::bigint[], m.id)
                """,
                (message_ids, message_ids),
            )
        )

    def list_recent_interval_summaries(self, limit: int = 24) -> list[dict[str, Any]]:
        entries_text = interval_summary_entries_text_sql("s")
        return list(
            self._execute(
                f"""
                SELECT 
                  s.id, s.summary_type, s.interval_start_date, s.period_days,
                  SUBSTRING({entries_text} FROM 1 FOR 400) as snippet,
                  s.summary_json,
                  b.name AS mailbox_name
                FROM mailbox_interval_summaries s
                JOIN mailboxes b ON b.id = s.mailbox_id
                WHERE s.refreshed_at IS NOT NULL
                  AND {entries_text} <> ''
                ORDER BY interval_start_date DESC, period_days DESC
                LIMIT %s
                """,
                (limit,),
            )
        )

    def list_mailbox_overview_interval_summaries(self, daily_limit: int = 8) -> list[dict[str, Any]]:
        entries_text = interval_summary_entries_text_sql("s")
        return list(
            self._execute(
                f"""
                WITH ranked AS (
                  SELECT
                    s.id, s.summary_type, s.interval_start_date, s.period_days,
                    SUBSTRING({entries_text} FROM 1 FOR 400) AS snippet,
                    s.summary_json,
                    b.name AS mailbox_name,
                    row_number() OVER (
                      PARTITION BY s.mailbox_id, s.summary_type
                      ORDER BY s.interval_start_date DESC, s.period_days DESC
                    ) AS mailbox_type_rank,
                    row_number() OVER (
                      PARTITION BY s.summary_type
                      ORDER BY s.interval_start_date DESC, s.period_days DESC
                    ) AS type_rank
                  FROM mailbox_interval_summaries s
                  JOIN mailboxes b ON b.id = s.mailbox_id
                  WHERE s.refreshed_at IS NOT NULL
                    AND {entries_text} <> ''
                )
                SELECT
                  id, summary_type, interval_start_date, period_days,
                  snippet, summary_json, mailbox_name
                FROM ranked
                WHERE (summary_type IN ('monthly', 'weekly') AND mailbox_type_rank = 1)
                   OR (summary_type = 'daily' AND type_rank <= %s)
                ORDER BY
                  CASE summary_type
                    WHEN 'monthly' THEN 1
                    WHEN 'weekly' THEN 2
                    ELSE 3
                  END,
                  interval_start_date DESC,
                  period_days DESC
                """,
                (daily_limit,),
            )
        )

    def vector_similarity(self, left: list[float] | str, right: list[float] | str) -> float:
        row = self._execute(
            """
            SELECT CASE
              WHEN vector_dims(%s::vector) = vector_dims(%s::vector)
              THEN 1.0 - (%s::vector <=> %s::vector)
              ELSE 0.0
            END AS score
            """,
            (_vector_param(left), _vector_param(right), _vector_param(left), _vector_param(right)),
        ).fetchone()
        return float(row["score"] or 0.0) if row else 0.0

    def vector_similarities(self, left_items: list[tuple[int, list[float] | str]], right: list[float] | str) -> dict[int, float]:
        if not left_items:
            return {}
        values_sql = ", ".join(["(%s, %s::vector)"] * len(left_items))
        params: list[object] = []
        for item_id, vector in left_items:
            params.extend([item_id, _vector_param(vector)])
        right_param = _vector_param(right)
        params.extend([right_param, right_param, right_param])
        rows = self._execute(
            f"""
            WITH candidates(item_id, embedding) AS (
              VALUES {values_sql}
            )
            SELECT
              item_id,
              CASE
                WHEN vector_dims(embedding) = vector_dims(%s::vector)
                THEN 1.0 - (embedding <=> %s::vector)
                ELSE 0.0
              END AS score
            FROM candidates
            """,
            tuple(params),
        )
        return {int(row["item_id"]): float(row["score"] or 0.0) for row in rows}

    def best_vector_match(self, vector: list[float] | str, candidates: list[list[float] | str]) -> dict[str, Any] | None:
        if not candidates:
            return None
        values_sql = ", ".join(["(%s, %s::vector)"] * len(candidates))
        params: list[object] = []
        for index, candidate in enumerate(candidates):
            params.extend([index, _vector_param(candidate)])
        query_vector = _vector_param(vector)
        params.extend([query_vector, query_vector, query_vector])
        return self._execute(
            f"""
            WITH candidates(candidate_index, embedding) AS (
              VALUES {values_sql}
            )
            SELECT
              candidate_index,
              1.0 - (embedding <=> %s::vector) AS score
            FROM candidates
            WHERE vector_dims(embedding) = vector_dims(%s::vector)
            ORDER BY embedding <=> %s::vector
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    def centroid_for_message_ids(self, message_ids: list[int], embedding_model: str) -> str:
        if not message_ids:
            return "[]"
        row = self._execute(
            """
            SELECT avg(me.embedding)::text AS centroid_text
            FROM message_embeddings me
            WHERE me.embedding_model = %s
              AND me.message_id = ANY(%s)
              AND me.status = 'ready'
            """,
            (embedding_model, message_ids),
        ).fetchone()
        return str(row["centroid_text"] or "[]") if row else "[]"

    def upsert_message_embedding(
        self,
        *,
        message_id: int,
        preset_name: str,
        embedding_model: str,
        input_text: str,
        vector: list[float],
    ) -> None:
        self._execute(
            """
            INSERT INTO message_embeddings (
              message_id, preset_name, embedding_model, input_hash, dimensions, status, embedding, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 'ready', %s::vector, now(), now())
            ON CONFLICT (message_id, embedding_model) DO UPDATE SET
              preset_name = EXCLUDED.preset_name,
              input_hash = EXCLUDED.input_hash,
              dimensions = EXCLUDED.dimensions,
              status = 'ready',
              embedding = EXCLUDED.embedding,
              updated_at = now()
            """,
            (message_id, preset_name, embedding_model, _hash_text(input_text), len(vector), _vector_literal(vector)),
        )
        self._commit()

    def list_ready_embeddings(self, embedding_model: str, limit: int = 1000) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  m.id, m.subject,
                  COALESCE(s.summary_text, '') AS summary_text,
                  COALESCE(bc.body_text, m.cleaned_text, '') AS cleaned_text,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  me.embedding::text AS embedding_text
                FROM message_embeddings me
                JOIN messages m ON m.id = me.message_id
                JOIN message_processing_state ps ON ps.message_id = me.message_id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                WHERE me.embedding_model = %s
                  AND me.status = 'ready'
                  AND ps.summary_status = 'done'
                  AND ps.embedding_status = 'done'
                  AND ps.embedding_model = me.embedding_model
                ORDER BY m.received_at DESC NULLS LAST
                LIMIT %s
                """,
                (embedding_model, limit),
            )
        )


    def initialize_incremental_clustering(self, embedding_model: str, algorithm_version: str) -> dict[str, int]:
        self._execute(
            """
            INSERT INTO message_clustering_work (
              message_id, embedding_model, algorithm_version, source_hash, status,
              lease_expires_at, retry_at, last_error_text, updated_at
            )
            SELECT
              me.message_id,
              me.embedding_model,
              %s,
              me.input_hash,
              CASE WHEN EXISTS (
                SELECT 1
                FROM message_topic_memberships mtm
                JOIN topic_clusters tc ON tc.id = mtm.topic_cluster_id
                WHERE mtm.message_id = me.message_id
                  AND tc.embedding_model = me.embedding_model
              ) THEN 'done' ELSE 'todo' END,
              NULL,
              NULL,
              '',
              now()
            FROM message_embeddings me
            JOIN message_processing_state ps ON ps.message_id = me.message_id
            WHERE me.embedding_model = %s
              AND me.status = 'ready'
              AND ps.summary_status = 'done'
              AND ps.embedding_status = 'done'
              AND ps.embedding_model = me.embedding_model
            ON CONFLICT (message_id, embedding_model) DO UPDATE SET
              algorithm_version = EXCLUDED.algorithm_version,
              source_hash = EXCLUDED.source_hash,
              status = CASE
                WHEN message_clustering_work.algorithm_version <> EXCLUDED.algorithm_version
                  OR message_clustering_work.source_hash <> EXCLUDED.source_hash
                THEN 'todo'
                WHEN message_clustering_work.status = 'done'
                  AND NOT EXISTS (
                    SELECT 1
                    FROM message_topic_memberships mtm
                    JOIN topic_clusters tc ON tc.id = mtm.topic_cluster_id
                    WHERE mtm.message_id = message_clustering_work.message_id
                      AND tc.embedding_model = EXCLUDED.embedding_model
                  )
                THEN 'todo'
                ELSE message_clustering_work.status
              END,
              lease_expires_at = CASE
                WHEN message_clustering_work.algorithm_version <> EXCLUDED.algorithm_version
                  OR message_clustering_work.source_hash <> EXCLUDED.source_hash
                THEN NULL ELSE message_clustering_work.lease_expires_at END,
              retry_at = CASE
                WHEN message_clustering_work.algorithm_version <> EXCLUDED.algorithm_version
                  OR message_clustering_work.source_hash <> EXCLUDED.source_hash
                THEN NULL ELSE message_clustering_work.retry_at END,
              last_error_text = CASE
                WHEN message_clustering_work.algorithm_version <> EXCLUDED.algorithm_version
                  OR message_clustering_work.source_hash <> EXCLUDED.source_hash
                THEN '' ELSE message_clustering_work.last_error_text END,
              updated_at = now()
            """,
            (algorithm_version, embedding_model),
        )
        self._commit()
        return self.get_incremental_clustering_work_counts(embedding_model, algorithm_version)

    def enqueue_incremental_clustering_message(
        self,
        message_id: int,
        embedding_model: str,
        algorithm_version: str,
        source_hash: str,
    ) -> None:
        self._execute(
            """
            INSERT INTO message_clustering_work (
              message_id, embedding_model, algorithm_version, source_hash, status,
              lease_expires_at, retry_at, last_error_text, updated_at
            )
            VALUES (%s, %s, %s, %s, 'todo', NULL, NULL, '', now())
            ON CONFLICT (message_id, embedding_model) DO UPDATE SET
              algorithm_version = EXCLUDED.algorithm_version,
              source_hash = EXCLUDED.source_hash,
              status = CASE
                WHEN message_clustering_work.algorithm_version = EXCLUDED.algorithm_version
                  AND message_clustering_work.source_hash = EXCLUDED.source_hash
                  AND message_clustering_work.status = 'done'
                  AND EXISTS (
                    SELECT 1
                    FROM message_topic_memberships mtm
                    JOIN topic_clusters tc ON tc.id = mtm.topic_cluster_id
                    WHERE mtm.message_id = EXCLUDED.message_id
                      AND tc.embedding_model = EXCLUDED.embedding_model
                  )
                THEN 'done' ELSE 'todo' END,
              lease_expires_at = NULL,
              retry_at = NULL,
              last_error_text = '',
              updated_at = now()
            """,
            (message_id, embedding_model, algorithm_version, source_hash),
        )
        self._commit()


    def get_incremental_clustering_message(self, message_id: int, embedding_model: str) -> dict[str, Any] | None:
        return self._execute(
            """
            SELECT
              m.id, m.subject, m.received_at,
              COALESCE(s.summary_text, '') AS summary_text,
              COALESCE(bc.body_text, m.cleaned_text, '') AS cleaned_text,
              COALESCE(s.importance_score, 0.0) AS importance_score,
              me.embedding::text AS embedding_text,
              me.dimensions AS embedding_dimensions,
              me.input_hash
            FROM messages m
            JOIN message_embeddings me
              ON me.message_id = m.id
             AND me.embedding_model = %s
             AND me.status = 'ready'
            JOIN message_processing_state ps ON ps.message_id = m.id
            LEFT JOIN message_body_cache bc ON bc.message_id = m.id
            LEFT JOIN message_summaries s ON s.message_id = m.id
            WHERE m.id = %s
              AND ps.summary_status = 'done'
              AND ps.embedding_status = 'done'
              AND ps.embedding_model = me.embedding_model
            """,
            (embedding_model, message_id),
        ).fetchone()

    def best_incremental_topic_cluster(
        self,
        message_id: int,
        embedding_model: str,
        *,
        embedding_text: str | None = None,
        dimensions: int | None = None,
    ) -> dict[str, Any] | None:
        if embedding_text is None or dimensions is None:
            target = self._execute(
                """SELECT embedding::text AS embedding_text, dimensions
                   FROM message_embeddings
                   WHERE message_id = %s AND embedding_model = %s AND status = 'ready'""",
                (message_id, embedding_model),
            ).fetchone()
            if target is None:
                return None
            embedding_text = str(target["embedding_text"])
            dimensions = int(target["dimensions"])
        return self._execute(
            """
            SELECT
              tc.id,
              tc.topic_key,
              tc.name,
              tc.frontier_at,
              1.0 - (tc.centroid <=> %s::vector) AS score
            FROM topic_clusters tc
            WHERE tc.embedding_model = %s
              AND tc.centroid IS NOT NULL
              AND tc.dimensions = %s
            ORDER BY tc.centroid <=> %s::vector, tc.id
            LIMIT 1
            """,
            (embedding_text, embedding_model, dimensions, embedding_text),
        ).fetchone()

    def assign_incremental_message_to_cluster(
        self, *, message_id: int, embedding_model: str, algorithm_version: str,
        source_hash: str, topic_cluster_id: int, score: float,
    ) -> int:
        old_rows = list(self._execute(
            "SELECT topic_cluster_id FROM message_topic_memberships WHERE message_id = %s",
            (message_id,),
        ))
        affected_cluster_ids = {topic_cluster_id, *(int(row["topic_cluster_id"]) for row in old_rows)}
        self._lock_incremental_topic_clusters(affected_cluster_ids)
        self._execute("DELETE FROM message_topic_memberships WHERE message_id = %s", (message_id,))
        self._execute(
            """INSERT INTO message_topic_memberships
               (message_id, topic_cluster_id, score, is_primary, assigned_at)
               VALUES (%s, %s, %s, true, now())""",
            (message_id, topic_cluster_id, max(0.1, score)),
        )
        self._refresh_incremental_topic_clusters(
            affected_cluster_ids, embedding_model=embedding_model,
            algorithm_version=algorithm_version,
        )
        completed = self._execute(
            """UPDATE message_clustering_work
               SET status = 'done', lease_expires_at = NULL, retry_at = NULL,
                   last_error_text = '', updated_at = now()
               WHERE message_id = %s AND embedding_model = %s
                 AND source_hash = %s AND algorithm_version = %s
                 AND status = 'in_progress'""",
            (message_id, embedding_model, source_hash, algorithm_version),
        )
        if completed.rowcount != 1:
            self.rollback()
            raise RuntimeError("Clustering input changed while assigning the message.")
        self._commit()
        return topic_cluster_id

    def publish_incremental_topic_cluster(
        self, *, message_id: int, embedding_model: str, algorithm_version: str,
        source_hash: str, cluster: TopicCluster, centroid: list[float],
        received_at: datetime | None,
    ) -> int:
        old_rows = list(self._execute(
            "SELECT topic_cluster_id FROM message_topic_memberships WHERE message_id = %s",
            (message_id,),
        ))
        self._lock_incremental_topic_clusters({int(row["topic_cluster_id"]) for row in old_rows})
        row = self._execute(
            """INSERT INTO topic_clusters (
                 topic_key, embedding_model, name, description,
                 key_terms_json, key_entities_json, llm_summary, why_it_matters,
                 importance_score, centroid, model_name, frontier_at, algorithm_version, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s, %s, now())
               ON CONFLICT (topic_key) DO UPDATE SET
                 centroid = EXCLUDED.centroid,
                 frontier_at = EXCLUDED.frontier_at,
                 algorithm_version = EXCLUDED.algorithm_version,
                 updated_at = now()
               RETURNING id""",
            (cluster.topic_key, embedding_model, cluster.name, cluster.description,
             _json(cluster.key_terms), _json(cluster.key_entities),
             cluster.llm_summary, cluster.why_it_matters, cluster.importance_score,
             _vector_literal(centroid), cluster.model_name, received_at, algorithm_version),
        ).fetchone()
        cluster_id = int(row["id"])
        self._execute("DELETE FROM message_topic_memberships WHERE message_id = %s", (message_id,))
        self._execute(
            """INSERT INTO message_topic_memberships
               (message_id, topic_cluster_id, score, is_primary, assigned_at)
               VALUES (%s, %s, 1.0, true, now())""",
            (message_id, cluster_id),
        )
        self._refresh_incremental_topic_clusters(
            {cluster_id, *(int(row["topic_cluster_id"]) for row in old_rows)},
            embedding_model=embedding_model, algorithm_version=algorithm_version,
        )
        completed = self._execute(
            """UPDATE message_clustering_work
               SET status = 'done', lease_expires_at = NULL, retry_at = NULL,
                   last_error_text = '', updated_at = now()
               WHERE message_id = %s AND embedding_model = %s
                 AND source_hash = %s AND algorithm_version = %s
                 AND status = 'in_progress'""",
            (message_id, embedding_model, source_hash, algorithm_version),
        )
        if completed.rowcount != 1:
            self.rollback()
            raise RuntimeError("Clustering input changed while publishing the message.")
        self._commit()
        return cluster_id

    def _lock_incremental_topic_clusters(self, cluster_ids: set[int]) -> None:
        for cluster_id in sorted(cluster_ids):
            self._execute("SELECT id FROM topic_clusters WHERE id = %s FOR UPDATE", (cluster_id,))

    def _refresh_incremental_topic_clusters(
        self,
        cluster_ids: set[int],
        *,
        embedding_model: str,
        algorithm_version: str,
    ) -> None:
        for cluster_id in sorted(cluster_ids):
            self._execute(
                """
                UPDATE topic_clusters tc
                SET centroid = source.centroid,
                    frontier_at = source.received_at,
                    algorithm_version = %s,
                    updated_at = now()
                FROM (
                  SELECT avg(me.embedding) AS centroid, max(m.received_at) AS received_at
                  FROM message_topic_memberships mtm
                  JOIN messages m ON m.id = mtm.message_id
                  JOIN message_embeddings me
                    ON me.message_id = mtm.message_id
                   AND me.embedding_model = %s
                   AND me.status = 'ready'
                  WHERE mtm.topic_cluster_id = %s
                ) source
                WHERE tc.id = %s
                  AND source.centroid IS NOT NULL
                """,
                (algorithm_version, embedding_model, cluster_id, cluster_id),
            )
            self._execute(
                """
                DELETE FROM topic_clusters tc
                WHERE tc.id = %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM message_topic_memberships mtm
                    WHERE mtm.topic_cluster_id = tc.id
                  )
                """,
                (cluster_id,),
            )

    def get_incremental_clustering_work_counts(
        self,
        embedding_model: str,
        algorithm_version: str,
    ) -> dict[str, int]:
        row = self._execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE work.status = 'todo') AS todo,
              COUNT(*) FILTER (WHERE work.status = 'in_progress') AS in_progress,
              COUNT(*) FILTER (WHERE work.status = 'done') AS done
            FROM message_clustering_work work
            JOIN message_embeddings me
              ON me.message_id = work.message_id
             AND me.embedding_model = work.embedding_model
             AND me.status = 'ready'
             AND me.input_hash = work.source_hash
            JOIN message_processing_state ps
              ON ps.message_id = work.message_id
             AND ps.summary_status = 'done'
             AND ps.embedding_status = 'done'
             AND ps.embedding_model = work.embedding_model
            WHERE work.embedding_model = %s AND work.algorithm_version = %s
            """,
            (embedding_model, algorithm_version),
        ).fetchone()
        return {
            "todo": int(row["todo"] or 0) if row else 0,
            "in_progress": int(row["in_progress"] or 0) if row else 0,
            "done": int(row["done"] or 0) if row else 0,
        }

    def embedding_input_for_row(self, row: dict[str, Any]) -> str:
        return embedding_input(
            str(row.get("subject") or ""),
            str(row.get("sender_name") or ""),
            str(row.get("sender_address") or ""),
            str(row.get("summary_text") or ""),
            str(row.get("cleaned_text") or ""),
        )

    def interval_summary_embedding_input_for_row(self, row: dict[str, Any]) -> str:
        title = f"{row.get('summary_type') or 'summary'} {row.get('interval_start_date') or ''}".strip()
        mailbox = str(row.get("mailbox_name") or row.get("provider_mailbox_id") or "")
        meta = f"Mailbox: {mailbox}\nPeriod days: {row.get('period_days') or ''}"
        entries_text = _interval_summary_entries_text(row.get("summary_json"))
        return embedding_input(title, mailbox, "", meta, entries_text)

    def upsert_interval_summary_embedding(
        self,
        *,
        summary_id: int,
        preset_name: str,
        embedding_model: str,
        input_text: str,
        vector: list[float],
    ) -> None:
        self._execute(
            """
            INSERT INTO interval_summary_embeddings (
              summary_id, preset_name, embedding_model, input_hash, dimensions, status, embedding, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 'ready', %s::vector, now(), now())
            ON CONFLICT (summary_id, embedding_model) DO UPDATE SET
              preset_name = EXCLUDED.preset_name,
              input_hash = EXCLUDED.input_hash,
              dimensions = EXCLUDED.dimensions,
              status = 'ready',
              embedding = EXCLUDED.embedding,
              updated_at = now()
            """,
            (summary_id, preset_name, embedding_model, _hash_text(input_text), len(vector), _vector_literal(vector)),
        )
        self._commit()

    def list_dashboard_topics(
        self, *, limit: int = 200, offset: int = 0,
        from_date: date | None = None, to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        date_clause, date_params = _date_range_clause("m", from_date, to_date)
        return list(self._execute(
            f"""SELECT cl.id, cl.name, cl.updated_at,
                       COUNT(DISTINCT mtm.message_id) AS message_count
                FROM topic_clusters cl
                JOIN message_topic_memberships mtm ON mtm.topic_cluster_id = cl.id
                JOIN messages m ON m.id = mtm.message_id
                WHERE cl.embedding_model = (
                  SELECT canonical_embedding_model FROM pipeline_settings WHERE id = 1
                ) {date_clause}
                GROUP BY cl.id
                ORDER BY cl.importance_score DESC, cl.name, cl.id
                LIMIT %s OFFSET %s""",
            (*date_params, limit, offset),
        ))

    def get_topic_detail(self, topic_id: int) -> dict[str, Any] | None:
        return self._execute(
            """SELECT cl.id, cl.name, cl.description, cl.key_terms_json,
                      cl.importance_score, cl.model_name, cl.embedding_model
               FROM topic_clusters cl
               WHERE cl.id = %s AND cl.embedding_model = (
                 SELECT canonical_embedding_model FROM pipeline_settings WHERE id = 1
               ) AND EXISTS (
                 SELECT 1 FROM message_topic_memberships mtm WHERE mtm.topic_cluster_id = cl.id
               )""",
            (topic_id,),
        ).fetchone()

    def rename_topic(self, topic_id: int, name: str) -> dict[str, Any] | None:
        row = self._execute(
            """UPDATE topic_clusters cl SET name = %s, updated_at = clock_timestamp()
               WHERE cl.id = %s AND EXISTS (
                 SELECT 1 FROM message_topic_memberships mtm WHERE mtm.topic_cluster_id = cl.id
               ) RETURNING id""",
            (name, topic_id),
        ).fetchone()
        self._commit()
        return self.get_topic_detail(topic_id) if row else None

    def relabel_topic_if_unchanged(
        self, topic_id: int, expected_name: str, name: str, description: str,
    ) -> bool:
        row = self._execute(
            """UPDATE topic_clusters cl
               SET name = %s, description = %s, updated_at = clock_timestamp()
               WHERE cl.id = %s AND cl.name = %s AND EXISTS (
                 SELECT 1 FROM message_topic_memberships mtm WHERE mtm.topic_cluster_id = cl.id
               ) RETURNING id""",
            (name, description, topic_id, expected_name),
        ).fetchone()
        self._commit()
        return row is not None

    def list_topic_label_samples(self, topic_id: int) -> list[dict[str, Any]]:
        return list(self._execute(
            """WITH members AS MATERIALIZED (
                 SELECT m.id, m.subject,
                        COALESCE(s.summary_text, m.cleaned_text, '') AS snippet,
                        m.received_at,
                        me.embedding <=> cl.centroid AS distance
                 FROM topic_clusters cl
                 JOIN message_topic_memberships mtm ON mtm.topic_cluster_id = cl.id
                 JOIN messages m ON m.id = mtm.message_id
                 JOIN message_embeddings me ON me.message_id = m.id
                   AND me.embedding_model = cl.embedding_model AND me.status = 'ready'
                 LEFT JOIN message_summaries s ON s.message_id = m.id
                 WHERE cl.id = %s
               ), chosen AS (
                 (SELECT * FROM members ORDER BY distance ASC, id LIMIT 4)
                 UNION
                 (SELECT * FROM members ORDER BY distance DESC, id LIMIT 4)
                 UNION
                 (SELECT * FROM members ORDER BY received_at DESC NULLS LAST, id DESC LIMIT 4)
               )
               SELECT id, subject, LEFT(snippet, 1800) AS snippet
               FROM chosen ORDER BY id LIMIT 12""",
            (topic_id,),
        ))

    def list_messages_for_topic(self, topic_id: int, limit: int = 50, *, offset: int = 0,
                                from_date: date | None = None, to_date: date | None = None,
    ) -> list[dict[str, Any]]:
        date_clause, date_params = _date_range_clause("m", from_date, to_date)
        return list(self._execute(
            f"""SELECT m.id, m.subject, m.sender_name, m.sender_address, m.received_at, m.is_read,
                  ma.name AS account_name, b.name AS mailbox_name, b.provider_mailbox_id,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  s.message_id IS NOT NULL AS has_summary,
                  COALESCE(s.rationale, '') AS rationale, COALESCE(s.model_name, '') AS model_name,
                  mtm.score AS topic_match_score,
                  EXISTS (SELECT 1 FROM message_embeddings me
                          WHERE me.message_id = m.id AND me.status = 'ready') AS has_embeddings
               FROM message_topic_memberships mtm
               JOIN messages m ON m.id = mtm.message_id
               JOIN topic_clusters cl ON cl.id = mtm.topic_cluster_id
               JOIN mailboxes b ON b.id = m.mailbox_id
               JOIN mail_accounts ma ON ma.id = b.account_id
               LEFT JOIN message_summaries s ON s.message_id = m.id
               LEFT JOIN message_body_cache bc ON bc.message_id = m.id
               WHERE cl.id = %s {date_clause}
               ORDER BY m.received_at DESC NULLS LAST, m.id DESC
               LIMIT %s OFFSET %s""",
            (topic_id, *date_params, limit, offset),
        ))

    def list_messages_for_topic_cluster(self, topic_cluster_id: int, limit: int = 10) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  m.id, m.subject, m.sender_name, m.sender_address, m.received_at, m.is_read,
                  ma.name AS account_name, b.name AS mailbox_name, b.provider_mailbox_id,
                  COALESCE(s.importance_score, 0.0) AS importance_score,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  CASE WHEN s.message_id IS NULL THEN false ELSE true END AS has_summary,
                  COALESCE(s.rationale, '') AS rationale,
                  COALESCE(s.model_name, '') AS model_name,
                  mtm.score AS topic_match_score,
                  mtm.is_primary,
                  CASE WHEN me.message_id IS NOT NULL THEN true ELSE false END AS has_embeddings
                FROM message_topic_memberships mtm
                JOIN messages m ON m.id = mtm.message_id
                JOIN mailboxes b ON b.id = m.mailbox_id
                JOIN mail_accounts ma ON ma.id = b.account_id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_embeddings me ON me.message_id = m.id AND me.status = 'ready'
                WHERE mtm.topic_cluster_id = %s
                ORDER BY mtm.is_primary DESC, mtm.score DESC, importance_score DESC, m.received_at DESC NULLS LAST
                LIMIT %s
                """,
                (topic_cluster_id, limit),
            )
        )

    def load_chat_sources(self, message_ids: list[int]) -> list[ChatSource]:
        if not message_ids:
            return []
        rows = list(
            self._execute(
                """
                SELECT
                  m.id,
                  m.subject,
                  m.sender_name,
                  m.sender_address,
                  m.received_at,
                  b.name AS mailbox_name,
                  COALESCE(s.summary_text, SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 400), '') AS snippet,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text
                FROM messages m
                JOIN mailboxes b ON b.id = m.mailbox_id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                WHERE m.id = ANY(%s)
                """,
                (message_ids,),
            )
        )
        by_id = {int(row["id"]): row for row in rows}
        return [
            ChatSource(
                type="message",
                id=message_id,
                title=str(by_id[message_id]["subject"]),
                snippet=str(by_id[message_id]["snippet"] or ""),
                date=str(by_id[message_id]["received_at"] or ""),
                mailbox=str(by_id[message_id]["mailbox_name"] or ""),
                subject=str(by_id[message_id]["subject"] or ""),
                sender_name=str(by_id[message_id]["sender_name"] or ""),
                sender_address=str(by_id[message_id]["sender_address"] or ""),
                received_at=str(by_id[message_id]["received_at"] or ""),
                summary_text=str(by_id[message_id]["summary_text"] or ""),
                preview_text=str(by_id[message_id]["preview_text"] or ""),
            )
            for message_id in message_ids
            if message_id in by_id
        ]

    def get_message_context_for_chat(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  m.id, m.mailbox_id, b.account_id, m.provider_message_id, m.subject,
                  COALESCE(s.summary_text, '') AS summary_text,
                  SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
                  m.received_at
                FROM messages m
                JOIN mailboxes b ON b.id = m.mailbox_id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                ORDER BY COALESCE(s.importance_score, 0.0) DESC, m.received_at DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
        )

    def list_stale_daily_interval_summaries(self, summary_timezone: str, limit: int = 25) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                WITH message_days AS (
                  SELECT
                    m.mailbox_id,
                    (m.received_at AT TIME ZONE %s)::date AS interval_start_date,
                    s.summarized_at
                  FROM messages m
                  JOIN message_summaries s ON s.message_id = m.id
                  WHERE m.received_at IS NOT NULL
                    AND s.summarized_at IS NOT NULL
                    AND s.summary_text <> ''
                ),
                summarized_days AS (
                  SELECT mailbox_id, interval_start_date, MAX(summarized_at) AS newest_summary_at
                  FROM message_days
                  GROUP BY mailbox_id, interval_start_date
                ),
                inserted AS (
                  INSERT INTO mailbox_interval_summaries (
                    mailbox_id, summary_type, interval_start_date, period_days, summary_json
                  )
                  SELECT mailbox_id, 'daily', interval_start_date, 1, '{"entries":[]}'::jsonb
                  FROM summarized_days
                  ON CONFLICT (mailbox_id, interval_start_date, period_days) DO NOTHING
                  RETURNING *
                ),
                stale_existing AS (
                  SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                  FROM summarized_days d
                  JOIN mailbox_interval_summaries s
                    ON s.mailbox_id = d.mailbox_id
                   AND s.interval_start_date = d.interval_start_date
                   AND s.period_days = 1
                  JOIN mailboxes b ON b.id = s.mailbox_id
                  WHERE s.refreshed_at IS NULL OR s.refreshed_at < d.newest_summary_at
                ),
                stale_inserted AS (
                  SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                  FROM inserted s
                  JOIN mailboxes b ON b.id = s.mailbox_id
                ),
                requested_existing AS (
                  SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                  FROM mailbox_interval_summaries s
                  JOIN mailboxes b ON b.id = s.mailbox_id
                  WHERE s.period_days = 1
                    AND s.request_rebuild IS NOT NULL
                ),
                stale AS (
                  SELECT * FROM stale_existing
                  UNION ALL
                  SELECT * FROM stale_inserted
                  UNION ALL
                  SELECT * FROM requested_existing
                ),
                deduped AS (
                  SELECT DISTINCT ON (id) *
                  FROM stale
                  ORDER BY
                    id,
                    (request_rebuild IS NOT NULL) DESC,
                    request_rebuild ASC NULLS LAST
                )
                SELECT * FROM deduped
                ORDER BY
                  (request_rebuild IS NOT NULL) DESC,
                  request_rebuild ASC NULLS LAST,
                  interval_start_date DESC,
                  (refreshed_at IS NULL) DESC,
                  mailbox_id
                LIMIT %s
                """,
                (summary_timezone, limit),
            )
        )

    def list_requested_interval_summaries(self, limit: int = 1000) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                FROM mailbox_interval_summaries s
                JOIN mailboxes b ON b.id = s.mailbox_id
                WHERE s.request_rebuild IS NOT NULL
                ORDER BY s.request_rebuild ASC, s.period_days ASC, s.interval_start_date DESC, s.mailbox_id
                LIMIT %s
                """,
                (limit,),
            )
        )

    def list_daily_summary_inputs(
        self,
        mailbox_id: int,
        interval_start_date: date,
        summary_timezone: str,
    ) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  m.id,
                  m.subject,
                  m.sender_name,
                  m.sender_address,
                  m.received_at,
                  s.summary_text,
                  s.summarized_at
                FROM messages m
                JOIN message_summaries s ON s.message_id = m.id
                WHERE m.mailbox_id = %s
                  AND (m.received_at AT TIME ZONE %s)::date = %s
                  AND s.summarized_at IS NOT NULL
                  AND s.summary_text <> ''
                ORDER BY m.received_at ASC NULLS LAST, m.id ASC
                """,
                (mailbox_id, summary_timezone, interval_start_date),
            )
        )

    def list_stale_rollup_interval_summaries(self, limit: int = 25) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                WITH daily_rows AS (
                  SELECT
                    mailbox_id,
                    interval_start_date,
                    refreshed_at,
                    interval_start_date - (((EXTRACT(ISODOW FROM interval_start_date)::integer + 6) %% 7) * interval '1 day') AS week_start_date,
                    date_trunc('month', interval_start_date)::date AS month_start_date
                  FROM mailbox_interval_summaries
                  WHERE period_days = 1
                    AND refreshed_at IS NOT NULL
                ),
                weekly_intervals AS (
                  SELECT mailbox_id, week_start_date::date AS interval_start_date, MAX(refreshed_at) AS newest_daily_at
                  FROM daily_rows
                  GROUP BY mailbox_id, week_start_date
                ),
                monthly_intervals AS (
                  SELECT
                    mailbox_id,
                    month_start_date AS interval_start_date,
                    (date_trunc('month', month_start_date) + interval '1 month - 1 day')::date AS month_end_date,
                    MAX(refreshed_at) AS newest_daily_at
                  FROM daily_rows
                  GROUP BY mailbox_id, month_start_date
                ),
                desired AS (
                  SELECT mailbox_id, 'weekly' AS summary_type, interval_start_date, 7 AS period_days, newest_daily_at
                  FROM weekly_intervals
                  UNION ALL
                  SELECT
                    mailbox_id,
                    'monthly' AS summary_type,
                    interval_start_date,
                    (month_end_date - interval_start_date + 1) AS period_days,
                    newest_daily_at
                  FROM monthly_intervals
                ),
                inserted AS (
                  INSERT INTO mailbox_interval_summaries (
                    mailbox_id, summary_type, interval_start_date, period_days, summary_json
                  )
                  SELECT mailbox_id, summary_type, interval_start_date, period_days, '{"entries":[]}'::jsonb
                  FROM desired
                  ON CONFLICT (mailbox_id, interval_start_date, period_days) DO NOTHING
                  RETURNING *
                ),
                stale_existing AS (
                  SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                  FROM desired d
                  JOIN mailbox_interval_summaries s
                    ON s.mailbox_id = d.mailbox_id
                   AND s.interval_start_date = d.interval_start_date
                   AND s.period_days = d.period_days
                  JOIN mailboxes b ON b.id = s.mailbox_id
                  WHERE s.refreshed_at IS NULL OR s.refreshed_at < d.newest_daily_at
                ),
                stale_inserted AS (
                  SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                  FROM inserted s
                  JOIN mailboxes b ON b.id = s.mailbox_id
                ),
                requested_existing AS (
                  SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
                  FROM mailbox_interval_summaries s
                  JOIN mailboxes b ON b.id = s.mailbox_id
                  WHERE s.period_days <> 1
                    AND s.request_rebuild IS NOT NULL
                ),
                stale AS (
                  SELECT * FROM stale_existing
                  UNION ALL
                  SELECT * FROM stale_inserted
                  UNION ALL
                  SELECT * FROM requested_existing
                ),
                deduped AS (
                  SELECT DISTINCT ON (id) *
                  FROM stale
                  ORDER BY
                    id,
                    (request_rebuild IS NOT NULL) DESC,
                    request_rebuild ASC NULLS LAST
                )
                SELECT * FROM deduped
                ORDER BY
                  (request_rebuild IS NOT NULL) DESC,
                  request_rebuild ASC NULLS LAST,
                  interval_start_date DESC,
                  period_days ASC,
                  mailbox_id
                LIMIT %s
                """,
                (limit,),
            )
        )

    def list_rollup_summary_inputs(self, mailbox_id: int, interval_start_date: date, period_days: int) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT interval_start_date, summary_json, refreshed_at
                FROM mailbox_interval_summaries
                WHERE mailbox_id = %s
                  AND period_days = 1
                  AND interval_start_date >= %s
                  AND interval_start_date < %s::date + (%s * interval '1 day')
                  AND refreshed_at IS NOT NULL
                ORDER BY interval_start_date ASC
                """,
                (mailbox_id, interval_start_date, interval_start_date, period_days),
            )
        )

    def mark_interval_summary_fresh(self, summary_id: int, summary_json: dict[str, Any]) -> None:
        self._execute(
            """
            UPDATE mailbox_interval_summaries
            SET summary_json = %s,
                refreshed_at = now(),
                request_rebuild = NULL,
                embedding_status = 'pending',
                embedding_needs_refresh = true,
                embedding_last_error = '',
                updated_at = now()
            WHERE id = %s
            """,
            (_json(summary_json), summary_id),
        )
        self._refresh_interval_summary_search_document(summary_id)
        self._commit()

    def mark_interval_summary_merge_fallback(self, summary_id: int, summary_json: dict[str, Any]) -> None:
        self._execute(
            """
            UPDATE mailbox_interval_summaries
            SET summary_json = %s,
                refreshed_at = NULL,
                embedding_status = 'pending',
                embedding_needs_refresh = true,
                embedding_last_error = '',
                updated_at = now()
            WHERE id = %s
            """,
            (_json(summary_json), summary_id),
        )
        self._refresh_interval_summary_search_document(summary_id)
        self._commit()

    def request_interval_summary_rebuild(self, interval_start_date: date, period_days: int, summary_type: str) -> list[dict[str, Any]]:
        rows = list(
            self._execute(
                """
                INSERT INTO mailbox_interval_summaries (
                  mailbox_id,
                  summary_type,
                  interval_start_date,
                  period_days,
                  refreshed_at,
                  request_rebuild,
                  summary_json
                )
                SELECT
                  id,
                  %s,
                  %s,
                  %s,
                  NULL,
                  now(),
                  '{"entries":[]}'::jsonb
                FROM mailboxes
                WHERE monitored = true
                ON CONFLICT (mailbox_id, interval_start_date, period_days) DO UPDATE SET
                  summary_type = EXCLUDED.summary_type,
                  refreshed_at = NULL,
                  request_rebuild = now(),
                  updated_at = now()
                RETURNING id, mailbox_id, summary_type, interval_start_date, period_days, request_rebuild
                """,
                (summary_type, interval_start_date, period_days),
            )
        )
        self._commit()
        return rows

    def list_interval_summaries(
        self,
        requests: list[tuple[date, int]],
        mailbox_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        if not requests:
            return []
        values_sql = ",".join(["(%s::date, %s::integer)"] * len(requests))
        params: list[object] = []
        for interval_start_date, period_days in requests:
            params.extend([interval_start_date, period_days])
        mailbox_filter = ""
        if mailbox_ids:
            mailbox_filter = "AND s.mailbox_id = ANY(%s::bigint[])"
            params.append(mailbox_ids)
        return list(
            self._execute(
                f"""
                WITH requested(interval_start_date, period_days) AS (
                  VALUES {values_sql}
                )
                SELECT
                  s.id,
                  s.mailbox_id,
                  ma.name AS account_name,
                  b.name AS mailbox_name,
                  b.provider_mailbox_id,
                  s.summary_type,
                  s.interval_start_date,
                  s.period_days,
                  s.refreshed_at,
                  s.request_rebuild,
                  s.summary_json
                FROM requested r
                JOIN mailbox_interval_summaries s
                  ON s.interval_start_date = r.interval_start_date
                 AND s.period_days = r.period_days
                JOIN mailboxes b ON b.id = s.mailbox_id
                JOIN mail_accounts ma ON ma.id = b.account_id
                WHERE true
                  {mailbox_filter}
                ORDER BY s.interval_start_date DESC, s.period_days DESC, s.mailbox_id
                """,
                tuple(params),
            )
        )

    def discard_unfinished_smart_searches(self) -> int:
        with self.transaction():
            self._queue_lock()
            rows = self._execute("""SELECT job_id FROM smart_search_jobs
                WHERE status IN ('queued', 'running')""").fetchall()
            for row in rows:
                self.queue_cancel_prefix(f"search:{row['job_id']}:", reason="Discarded on application startup")
            if not rows:
                return 0
            cursor = self._execute("DELETE FROM jobs WHERE id = ANY(%s)",
                                   ([row['job_id'] for row in rows],))
            return cursor.rowcount

    def create_smart_search_job(
        self,
        *,
        question: str,
        idempotency_key: str | None,
        planner_preset_id: int,
        embedding_preset_id: int,
        compressor_preset_id: int,
        answer_preset_id: int,
        embedding_model: str,
        owner_timezone: str,
        preset_fingerprints: dict[str, str],
    ) -> int:
        if idempotency_key:
            self._execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"smart-search:{idempotency_key}",),
            )
            existing = self._execute(
                "SELECT job_id, question FROM smart_search_jobs WHERE idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if str(existing["question"]) != question:
                    self.rollback()
                    raise ValueError("idempotency key was already used for another search")
                self._commit()
                return int(existing["job_id"])
        row = self._execute(
            """
            WITH created_job AS (
              INSERT INTO jobs (
                job_type, status, phase, total_count, processed_count,
                current_item, error_text, metadata_json
              )
              VALUES ('smart_search', 'running', 'queued', 7, 0, '', '', '{}'::jsonb)
              RETURNING id
            )
            INSERT INTO smart_search_jobs (
              job_id, question, idempotency_key, status,
              planner_preset_id, embedding_preset_id,
              compressor_preset_id, answer_preset_id, embedding_model,
              owner_timezone, preset_fingerprints_json
            )
            SELECT
              id, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s
            FROM created_job
            RETURNING job_id
            """,
            (
                question,
                idempotency_key,
                planner_preset_id,
                embedding_preset_id,
                compressor_preset_id,
                answer_preset_id,
                embedding_model,
                owner_timezone,
                _json(preset_fingerprints),
            ),
        ).fetchone()
        self._commit()
        return int(row["job_id"])


    def get_smart_search_job(self, job_id: int) -> dict[str, Any] | None:
        return self._execute(
            """
            SELECT
              sj.*,
              j.phase,
              j.total_count,
              j.processed_count,
              j.current_item
            FROM smart_search_jobs sj
            JOIN jobs j ON j.id = sj.job_id
            WHERE sj.job_id = %s
            """,
            (job_id,),
        ).fetchone()

    def list_smart_search_jobs(self, *, limit: int, before_id: int | None = None) -> list[dict[str, Any]]:
        return list(self._execute(
            """
            SELECT job_id, question, status, retryable, created_at, completed_at
            FROM smart_search_jobs
            WHERE status <> 'expired'
              AND (%s::bigint IS NULL OR job_id < %s)
            ORDER BY job_id DESC
            LIMIT %s
            """,
            (before_id, before_id, limit),
        ).fetchall())

    def delete_smart_search_job(self, job_id: int) -> bool:
        row = self._execute(
            """
            DELETE FROM jobs j
            USING smart_search_jobs sj
            WHERE j.id = sj.job_id
              AND sj.job_id = %s
              AND sj.status NOT IN ('queued', 'running')
            RETURNING j.id
            """,
            (job_id,),
        ).fetchone()
        self._commit()
        return row is not None



    def smart_search_cancel_requested(self, job_id: int) -> bool:
        row = self._execute(
            "SELECT cancel_requested, status FROM smart_search_jobs WHERE job_id = %s",
            (job_id,),
        ).fetchone()
        return bool(row is None or row["cancel_requested"] or str(row["status"]) == "cancelled")

    def cancel_smart_search_job(self, job_id: int) -> dict[str, Any] | None:
        row = self._execute(
            """
            UPDATE smart_search_jobs
            SET cancel_requested = true,
                status = 'cancelled',
                completed_at = now(),
                lease_expires_at = NULL,
                updated_at = now()
            WHERE job_id = %s
              AND status IN ('queued', 'running')
            RETURNING status
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            existing = self.get_smart_search_job(job_id)
            if existing is None:
                self._commit()
                return None
            self._commit()
            return existing
        self._execute("DELETE FROM smart_search_job_messages WHERE job_id = %s", (job_id,))
        self._execute(
            """
            UPDATE jobs
            SET status = 'cancelled', phase = 'cancelled',
                completed_at = now(), updated_at = now()
            WHERE id = %s
            """,
            (job_id,),
        )
        self._commit()
        return self.get_smart_search_job(job_id)

    def update_smart_search_stage(
        self,
        job_id: int,
        *,
        stage: str,
        stage_state: str,
        stage_trace: dict[str, Any],
        processed_count: int,
        plan: dict[str, Any] | None = None,
        coverage: dict[str, Any] | None = None,
        embedding_dimensions: int | None = None,
    ) -> None:
        self._execute(
            """
            UPDATE smart_search_jobs
            SET stage_trace_json = %s,
                plan_json = COALESCE(%s, plan_json),
                coverage_json = COALESCE(%s, coverage_json),
                embedding_dimensions = COALESCE(%s, embedding_dimensions),
                updated_at = now()
            WHERE job_id = %s AND status = 'running'
            """,
            (
                _json(stage_trace),
                _json(plan) if plan is not None else None,
                _json(coverage) if coverage is not None else None,
                embedding_dimensions,
                job_id,
            ),
        )
        self._execute(
            """
            UPDATE jobs j
            SET phase = %s,
                processed_count = %s,
                current_item = %s,
                updated_at = now()
            FROM smart_search_jobs sj
            WHERE j.id = %s
              AND sj.job_id = j.id
              AND sj.status = 'running'
            """,
            (stage, processed_count, stage_state, job_id),
        )
        self._commit()

    def fail_smart_search_job(
        self,
        job_id: int,
        *,
        failed_stage: str,
        error_code: str,
        safe_error_text: str,
        retryable: bool,
        stage_trace: dict[str, Any],
    ) -> None:
        self._execute(
            """
            UPDATE smart_search_jobs
            SET status = CASE WHEN cancel_requested THEN 'cancelled' ELSE 'failed' END,
                failed_stage = CASE WHEN cancel_requested THEN NULL ELSE %s END,
                error_code = CASE WHEN cancel_requested THEN NULL ELSE %s END,
                safe_error_text = CASE WHEN cancel_requested THEN '' ELSE %s END,
                retryable = CASE WHEN cancel_requested THEN false ELSE %s END,
                stage_trace_json = %s,
                lease_expires_at = NULL,
                completed_at = now(),
                updated_at = now()
            WHERE job_id = %s AND status = 'running'
            """,
            (failed_stage, error_code, safe_error_text, retryable, _json(stage_trace), job_id),
        )
        self._execute(
            """
            UPDATE jobs j
            SET status = CASE
                  WHEN sj.status = 'cancelled' THEN 'cancelled'
                  ELSE 'failed'
                END,
                phase = CASE
                  WHEN sj.status = 'cancelled' THEN 'cancelled'
                  ELSE %s
                END,
                error_text = CASE
                  WHEN sj.status = 'cancelled' THEN ''
                  ELSE %s
                END,
                completed_at = now(),
                updated_at = now()
            FROM smart_search_jobs sj
            WHERE j.id = sj.job_id
              AND j.id = %s
              AND sj.status IN ('failed', 'cancelled')
            """,
            (failed_stage, safe_error_text, job_id),
        )
        self._execute("DELETE FROM smart_search_job_messages WHERE job_id = %s", (job_id,))
        self._commit()

    def complete_smart_search_job(
        self,
        job_id: int,
        *,
        result: dict[str, Any],
        coverage: dict[str, Any],
        stage_trace: dict[str, Any],
    ) -> bool:
        row = self._execute(
            """
            UPDATE smart_search_jobs
            SET status = 'succeeded',
                result_json = %s,
                coverage_json = %s,
                stage_trace_json = %s,
                lease_expires_at = NULL,
                completed_at = now(),
                updated_at = now()
            WHERE job_id = %s
              AND status = 'running'
              AND cancel_requested = false
            RETURNING job_id
            """,
            (_json(result), _json(coverage), _json(stage_trace), job_id),
        ).fetchone()
        if row is not None:
            self._execute("DELETE FROM smart_search_job_messages WHERE job_id = %s", (job_id,))
            self._execute(
                """
                UPDATE jobs
                SET status = 'completed', phase = 'completed',
                    processed_count = total_count, current_item = '',
                    completed_at = now(), updated_at = now()
                WHERE id = %s
                """,
                (job_id,),
            )
        else:
            cancelled = self._execute(
                """
                UPDATE smart_search_jobs
                SET status = 'cancelled', lease_expires_at = NULL,
                    completed_at = now(), updated_at = now()
                WHERE job_id = %s AND status = 'running' AND cancel_requested = true
                RETURNING job_id
                """,
                (job_id,),
            ).fetchone()
            if cancelled is not None:
                self._execute("DELETE FROM smart_search_job_messages WHERE job_id = %s", (job_id,))
                self._execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', phase = 'cancelled',
                        completed_at = now(), updated_at = now()
                    WHERE id = %s
                    """,
                    (job_id,),
                )
        self._commit()
        return row is not None

    def snapshot_smart_search_corpus(self, job_id: int, *, embedding_model: str) -> dict[str, Any]:
        self._execute("DELETE FROM smart_search_job_messages WHERE job_id = %s", (job_id,))
        inserted = self._execute(
            """
            WITH active_job AS MATERIALIZED (
              SELECT job_id
              FROM smart_search_jobs
              WHERE job_id = %s
                AND status = 'running'
                AND cancel_requested = false
              FOR UPDATE
            )
            INSERT INTO smart_search_job_messages (job_id, message_id, source_hash)
            SELECT active_job.job_id, m.id, ps.source_input_hash
            FROM active_job
            CROSS JOIN messages m
            JOIN message_processing_state ps ON ps.message_id = m.id
            JOIN message_embeddings me
              ON me.message_id = m.id
             AND me.embedding_model = %s
            WHERE me.status = 'ready'
              AND me.embedding IS NOT NULL
              AND ps.embedding_status = 'done'
              AND ps.embedding_model = me.embedding_model
              AND ps.embedding_input_hash = me.input_hash
            ON CONFLICT (job_id, message_id) DO NOTHING
            """,
            (job_id, embedding_model),
        ).rowcount
        dimensions = list(
            self._execute(
                """
                SELECT DISTINCT me.dimensions
                FROM smart_search_job_messages sm
                JOIN message_embeddings me
                  ON me.message_id = sm.message_id
                 AND me.embedding_model = %s
                WHERE sm.job_id = %s
                ORDER BY me.dimensions
                """,
                (embedding_model, job_id),
            )
        )
        counts = self._execute(
            """
            SELECT
              COUNT(*) AS total_message_count,
              COUNT(*) FILTER (WHERE ps.message_id IS NULL) AS unprocessed_embedding_count,
              COUNT(*) FILTER (
                WHERE ps.embedding_status = 'pending'
                   OR ps.embedding_status = 'running'
              ) AS pending_embedding_count,
              COUNT(*) FILTER (WHERE ps.embedding_status = 'failed') AS failed_embedding_count,
              COUNT(*) FILTER (
                WHERE ps.embedding_status = 'done'
                  AND ps.embedding_model <> %s
              ) AS differently_modeled_embedding_count,
              COUNT(*) FILTER (
                WHERE ps.embedding_status = 'done'
                  AND ps.embedding_model = %s
                  AND NOT EXISTS (
                    SELECT 1
                    FROM message_embeddings me
                    WHERE me.message_id = m.id
                      AND me.embedding_model = %s
                      AND me.status = 'ready'
                      AND me.input_hash = ps.embedding_input_hash
                  )
              ) AS stale_embedding_count
            FROM messages m
            LEFT JOIN message_processing_state ps ON ps.message_id = m.id
            """,
            (embedding_model, embedding_model, embedding_model),
        ).fetchone()
        self._commit()
        return {
            "eligible_message_count": int(inserted),
            "embedding_dimensions": [int(row["dimensions"]) for row in dimensions],
            "total_message_count": int(counts["total_message_count"] or 0),
            "unprocessed_embedding_count": int(counts["unprocessed_embedding_count"] or 0),
            "pending_embedding_count": int(counts["pending_embedding_count"] or 0),
            "failed_embedding_count": int(counts["failed_embedding_count"] or 0),
            "differently_modeled_embedding_count": int(
                counts["differently_modeled_embedding_count"] or 0
            ),
            "stale_embedding_count": int(counts["stale_embedding_count"] or 0),
        }

    def begin_smart_search_snapshot_read(
        self,
        job_id: int,
        *,
        embedding_model: str,
        embedding_dimensions: int,
    ) -> None:
        self._execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        row = self._execute(
            """
            SELECT COUNT(*) AS changed_count
            FROM smart_search_job_messages sm
            LEFT JOIN message_processing_state ps ON ps.message_id = sm.message_id
            LEFT JOIN message_embeddings me
              ON me.message_id = sm.message_id
             AND me.embedding_model = %s
            WHERE sm.job_id = %s
              AND (
                ps.message_id IS NULL
                OR ps.source_input_hash <> sm.source_hash
                OR ps.embedding_status <> 'done'
                OR ps.embedding_model <> %s
                OR me.message_id IS NULL
                OR me.status <> 'ready'
                OR me.embedding IS NULL
                OR me.dimensions <> %s
                OR me.input_hash <> ps.embedding_input_hash
              )
            """,
            (embedding_model, job_id, embedding_model, embedding_dimensions),
        ).fetchone()
        if int(row["changed_count"] or 0):
            raise RuntimeError("smart-search corpus changed after it was pinned")

    def smart_search_mailbox_ids_exist(self, mailbox_ids: list[int]) -> bool:
        if not mailbox_ids:
            return True
        row = self._execute(
            "SELECT COUNT(*) AS matched FROM mailboxes WHERE id = ANY(%s)",
            (mailbox_ids,),
        ).fetchone()
        return int(row["matched"] or 0) == len(set(mailbox_ids))

    def smart_search_indexes_ready(self, embedding_dimensions: int) -> bool:
        if embedding_dimensions != 1024:
            return False
        row = self._execute(
            """
            SELECT
              EXISTS (
                SELECT 1
                FROM pg_index
                WHERE indexrelid = to_regclass('idx_messages_search_document')
                  AND indisvalid
                  AND indisready
              ) AS fts_ready,
              EXISTS (
                SELECT 1
                FROM pg_index
                WHERE indexrelid = to_regclass('idx_embeddings_hnsw_1024_cosine')
                  AND indisvalid
                  AND indisready
              ) AS vector_ready
            """
        ).fetchone()
        return bool(row and row["fts_ready"] and row["vector_ready"])

    def search_smart_search_preview(self, *, keyword_terms: list[str], limit: int,
                                  statement_timeout_ms: int, embedding_model: str = "",
                                  query_vector: list[float] | None = None) -> list[dict[str, Any]]:
        """Independent, read-only early retrieval; lexical mail needs no embedding."""
        self._execute("SELECT set_config('statement_timeout', %s, true)",
                      (f"{max(statement_timeout_ms, 1)}ms",))
        if query_vector:
            dimension = len(query_vector)
            distance = ("me.embedding::vector(1024) <=> %s::vector(1024)" if dimension == 1024
                        else "me.embedding <=> %s::vector")
            candidates = f"""SELECT me.message_id AS id, 0::bigint AS keyword_match_count,
                row_number() OVER (ORDER BY {distance}, me.message_id) AS rank
                FROM message_embeddings me JOIN message_processing_state ps ON ps.message_id=me.message_id
                WHERE me.embedding_model=%s AND me.dimensions=%s AND me.status='ready'
                  AND ps.embedding_status='done' AND ps.embedding_model=me.embedding_model
                  AND ps.embedding_input_hash=me.input_hash
                ORDER BY rank LIMIT %s"""
            params = (_vector_literal(query_vector), embedding_model, dimension, limit)
        else:
            candidates = """WITH needles AS (
                  SELECT DISTINCT phraseto_tsquery('simple', term) AS query
                  FROM unnest(%s::text[]) AS term WHERE numnode(phraseto_tsquery('simple', term))>0
                )
                SELECT m.id, count(*) AS keyword_match_count,
                  row_number() OVER (ORDER BY count(*) DESC, m.received_at DESC NULLS LAST, m.id) AS rank
                FROM needles n JOIN messages m ON m.search_document @@ n.query
                GROUP BY m.id ORDER BY rank LIMIT %s"""
            params = (keyword_terms, limit)
        return list(self._execute(f"""WITH candidates AS ({candidates})
            SELECT m.id, m.subject, m.sender_name, m.sender_address, m.received_at,
              b.name AS mailbox_name, left(COALESCE(bc.body_text,m.cleaned_text,''), 500) AS body_text,
              c.keyword_match_count
            FROM candidates c JOIN messages m ON m.id=c.id JOIN mailboxes b ON b.id=m.mailbox_id
            LEFT JOIN message_body_cache bc ON bc.message_id=m.id ORDER BY c.rank""", params))

    def merge_smart_search_matches(self, job_id: int, sources: list[dict[str, Any]]) -> None:
        row = self._execute("SELECT matching_sources_json, rejected_message_ids_json FROM smart_search_jobs WHERE job_id=%s FOR UPDATE",
                            (job_id,)).fetchone()
        if not row:
            return
        # Each of the three retrieval passes is bounded. Keep earlier matches,
        # even when a later pass selects different evidence for the answer.
        merged = {int(source["id"]): source for source in row["matching_sources_json"] or []}
        for source in sources:
            previous = merged.get(int(source["id"]), {})
            merged[int(source["id"])] = {**source,
                "keyword_match_count": max(int(source.get("keyword_match_count", 0)),
                                           int(previous.get("keyword_match_count", 0)))}
        rejected = set(row["rejected_message_ids_json"] or [])
        ordered = sorted((source for mid, source in merged.items() if mid not in rejected),
                         key=lambda source: -int(source.get("keyword_match_count", 0)))
        self._execute("UPDATE smart_search_jobs SET matching_sources_json=%s, updated_at=now() WHERE job_id=%s",
                      (_json(ordered), job_id))

    def reject_smart_search_matches(self, job_id: int, message_ids: list[int]) -> None:
        row = self._execute("SELECT rejected_message_ids_json FROM smart_search_jobs WHERE job_id=%s FOR UPDATE",
                            (job_id,)).fetchone()
        if not row:
            return
        rejected = sorted(set(row["rejected_message_ids_json"] or []) | set(message_ids))
        self._execute("UPDATE smart_search_jobs SET rejected_message_ids_json=%s WHERE job_id=%s",
                      (_json(rejected), job_id))
        self.merge_smart_search_matches(job_id, [])

    def search_smart_search_snapshot(
        self,
        job_id: int,
        *,
        embedding_model: str,
        query_text: str,
        query_vector: list[float],
        exact_phrases: list[str] | None,
        keyword_terms: list[str] | None = None,
        prefer_recent: bool = False,
        limit: int,
        statement_timeout_ms: int,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
        sender_addresses: list[str] | None = None,
        mailbox_ids: list[int] | None = None,
        subject_terms: list[str] | None = None,
        is_read: bool | None = None,
        has_attachments: bool | None = None,
    ) -> list[dict[str, Any]]:
        filters = ["sm.job_id = %s", "ps.source_input_hash = sm.source_hash"]
        params: list[object] = [job_id]
        if received_from is not None:
            filters.append("m.received_at >= %s")
            params.append(received_from)
        if received_to is not None:
            filters.append("m.received_at < %s")
            params.append(received_to)
        if sender_addresses:
            filters.append("lower(m.sender_address) = ANY(%s)")
            params.append([value.lower() for value in sender_addresses])
        if mailbox_ids:
            filters.append("m.mailbox_id = ANY(%s)")
            params.append(mailbox_ids)
        if subject_terms:
            for value in subject_terms:
                escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                filters.append("m.subject ILIKE %s ESCAPE '\\'")
                params.append(f"%{escaped}%")
        if is_read is not None:
            filters.append("m.is_read = %s")
            params.append(is_read)
        if has_attachments is not None:
            filters.append("m.has_attachments = %s")
            params.append(has_attachments)
        filter_sql = " AND ".join(filters)
        lane_limit = max(limit * 2, limit)
        # Preserve planner-provided phrase boundaries. Fallback callers may use
        # quotes to group phrases; unquoted words remain independent keywords.
        terms = keyword_terms if keyword_terms is not None else [
            phrase or word for phrase, word in re.findall(r'"([^\"]+)"|(\S+)', query_text)
        ]
        keyword_units = list(dict.fromkeys(
            normalized for term in [*terms, *(exact_phrases or [])]
            if (normalized := " ".join(term.split()).casefold())
        ))
        vector_distance_sql = (
            "embedding::vector(1024) <=> %s::vector(1024)"
            if len(query_vector) == 1024
            else "embedding <=> %s::vector"
        )
        self._execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{max(statement_timeout_ms, 1)}ms",),
        )
        rows = list(
            self._execute(
                f"""
                WITH eligible AS NOT MATERIALIZED (
                  SELECT
                    m.id, m.mailbox_id, m.subject, m.sender_name, m.sender_address,
                    m.received_at, m.is_read, m.has_attachments, m.search_document,
                    b.name AS mailbox_name,
                    COALESCE(s.summary_text, '') AS summary_text,
                    COALESCE(bc.body_text, m.cleaned_text, '') AS body_text,
                    me.embedding
                  FROM smart_search_job_messages sm
                  JOIN messages m ON m.id = sm.message_id
                  JOIN message_processing_state ps ON ps.message_id = m.id
                  JOIN message_embeddings me
                    ON me.message_id = m.id
                   AND me.embedding_model = %s
                   AND me.status = 'ready'
                   AND me.dimensions = %s
                  JOIN mailboxes b ON b.id = m.mailbox_id
                  LEFT JOIN message_summaries s ON s.message_id = m.id
                  LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                  WHERE {filter_sql}
                ),
                keyword_queries AS (
                  SELECT DISTINCT phraseto_tsquery('simple', unit) AS query
                  FROM unnest(%s::text[]) AS unit
                  WHERE numnode(phraseto_tsquery('simple', unit)) > 0
                ),
                keyword_matches AS (
                  SELECT e.id, COUNT(*) AS keyword_match_count
                  FROM eligible e
                  JOIN keyword_queries k ON e.search_document @@ k.query
                  GROUP BY e.id
                ),
                lexical AS (
                  SELECT id, row_number() OVER (
                    ORDER BY keyword_match_count DESC,
                             received_at DESC NULLS LAST,
                             id
                  ) AS lane_rank
                  FROM eligible
                  JOIN keyword_matches USING (id)
                  ORDER BY lane_rank
                  LIMIT %s
                ),
                semantic AS (
                  SELECT id, row_number() OVER (
                    ORDER BY {vector_distance_sql}, id
                  ) AS lane_rank
                  FROM eligible
                  WHERE vector_dims(embedding) = %s
                  LIMIT %s
                ),
                exact_match AS (
                  SELECT id, row_number() OVER (
                    ORDER BY received_at DESC NULLS LAST, id
                  ) AS lane_rank
                  FROM eligible
                  WHERE cardinality(%s::text[]) > 0
                    AND EXISTS (
                      SELECT 1
                      FROM unnest(%s::text[]) AS phrase
                      WHERE search_document @@ phraseto_tsquery('simple', phrase)
                    )
                  LIMIT %s
                ),
                recent_topical AS (
                  SELECT id, row_number() OVER (
                    ORDER BY received_at DESC NULLS LAST, id
                  ) AS lane_rank
                  FROM eligible
                  JOIN keyword_matches USING (id)
                  WHERE %s
                  ORDER BY lane_rank
                  LIMIT %s
                ),
                fused AS (
                  SELECT
                    id,
                    SUM(score) AS rrf_score,
                    MIN(lexical_rank) AS lexical_rank,
                    MIN(semantic_rank) AS semantic_rank,
                    MIN(exact_rank) AS exact_rank
                  FROM (
                    SELECT id, 1.0 / (60.0 + lane_rank) AS score,
                           lane_rank AS lexical_rank, NULL::bigint AS semantic_rank,
                           NULL::bigint AS exact_rank
                    FROM lexical
                    UNION ALL
                    SELECT id, 1.0 / (60.0 + lane_rank) AS score,
                           NULL::bigint, lane_rank, NULL::bigint
                    FROM semantic
                    UNION ALL
                    SELECT id, 1.0 / (60.0 + lane_rank) AS score,
                           NULL::bigint, NULL::bigint, lane_rank
                    FROM exact_match
                    UNION ALL
                    SELECT id, 2.0 / (60.0 + lane_rank) AS score,
                           NULL::bigint, NULL::bigint, NULL::bigint
                    FROM recent_topical
                  ) lanes
                  GROUP BY id
                )
                SELECT
                  e.id, e.mailbox_id, e.subject, e.sender_name, e.sender_address,
                  e.received_at, e.is_read, e.has_attachments, e.mailbox_name,
                  e.summary_text, e.body_text,
                  f.rrf_score, f.lexical_rank, f.semantic_rank, f.exact_rank,
                  COALESCE(k.keyword_match_count, 0) AS keyword_match_count
                FROM fused f
                JOIN eligible e ON e.id = f.id
                LEFT JOIN keyword_matches k ON k.id = f.id
                ORDER BY COALESCE(k.keyword_match_count, 0) DESC,
                         f.rrf_score DESC, e.received_at DESC NULLS LAST, e.id
                LIMIT %s
                """,
                (
                    embedding_model,
                    len(query_vector),
                    *params,
                    keyword_units,
                    lane_limit,
                    _vector_literal(query_vector),
                    len(query_vector),
                    lane_limit,
                    exact_phrases or [],
                    exact_phrases or [],
                    lane_limit,
                    prefer_recent,
                    lane_limit,
                    limit,
                ),
            )
        )
        return rows

    def list_smart_search_snapshot(
        self,
        job_id: int,
        *,
        embedding_model: str,
        limit: int,
        statement_timeout_ms: int,
        received_from: datetime | None = None,
        received_to: datetime | None = None,
        sender_addresses: list[str] | None = None,
        mailbox_ids: list[int] | None = None,
        subject_terms: list[str] | None = None,
        is_read: bool | None = None,
        has_attachments: bool | None = None,
    ) -> list[dict[str, Any]]:
        filters = ["sm.job_id = %s", "ps.source_input_hash = sm.source_hash"]
        params: list[object] = [job_id]
        if received_from is not None:
            filters.append("m.received_at >= %s")
            params.append(received_from)
        if received_to is not None:
            filters.append("m.received_at < %s")
            params.append(received_to)
        if sender_addresses:
            filters.append("lower(m.sender_address) = ANY(%s)")
            params.append([value.lower() for value in sender_addresses])
        if mailbox_ids:
            filters.append("m.mailbox_id = ANY(%s)")
            params.append(mailbox_ids)
        if subject_terms:
            for value in subject_terms:
                escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                filters.append("m.subject ILIKE %s ESCAPE '\\'")
                params.append(f"%{escaped}%")
        if is_read is not None:
            filters.append("m.is_read = %s")
            params.append(is_read)
        if has_attachments is not None:
            filters.append("m.has_attachments = %s")
            params.append(has_attachments)
        self._execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{max(statement_timeout_ms, 1)}ms",),
        )
        return list(
            self._execute(
                f"""
                SELECT
                  m.id, m.mailbox_id, m.subject, m.sender_name, m.sender_address,
                  m.received_at, m.is_read, m.has_attachments, b.name AS mailbox_name,
                  COALESCE(s.summary_text, '') AS summary_text,
                  COALESCE(bc.body_text, m.cleaned_text, '') AS body_text,
                  NULL::double precision AS rrf_score,
                  NULL::bigint AS lexical_rank,
                  NULL::bigint AS semantic_rank,
                  NULL::bigint AS exact_rank
                FROM smart_search_job_messages sm
                JOIN messages m ON m.id = sm.message_id
                JOIN message_processing_state ps ON ps.message_id = m.id
                JOIN message_embeddings me
                  ON me.message_id = m.id
                 AND me.embedding_model = %s
                JOIN mailboxes b ON b.id = m.mailbox_id
                LEFT JOIN message_summaries s ON s.message_id = m.id
                LEFT JOIN message_body_cache bc ON bc.message_id = m.id
                WHERE {" AND ".join(filters)}
                ORDER BY m.received_at DESC NULLS LAST, m.id
                LIMIT %s
                """,
                (embedding_model, *params, max(limit, 1)),
            )
        )

    def delete_expired_smart_search_payloads(self, *, older_than: datetime) -> int:
        cursor = self._execute(
            """
            DELETE FROM jobs j
            USING smart_search_jobs sj
            WHERE j.id = sj.job_id
              AND (sj.status = 'expired' OR (
                sj.status IN ('succeeded', 'failed', 'cancelled')
                AND sj.completed_at < %s
              ))
            """,
            (older_than,),
        )
        self._commit()
        return cursor.rowcount

    def create_job(self, job: JobRecord) -> int:
        row = self._execute(
            """
            INSERT INTO jobs (job_type, status, phase, total_count, processed_count, current_item, error_text, metadata_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                job.job_type,
                job.status,
                job.phase,
                job.total_count,
                job.processed_count,
                job.current_item,
                job.error_text,
                _json(job.metadata),
            ),
        ).fetchone()
        self._commit()
        return int(row["id"])

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        phase: str | None = None,
        total_count: int | None = None,
        processed_count: int | None = None,
        current_item: str | None = None,
        error_text: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        completed = ", completed_at = now()" if status in {"completed", "failed", "cancelled"} else ""
        self._execute(
            f"""
            UPDATE jobs
            SET status = COALESCE(%s, status),
                phase = COALESCE(%s, phase),
                total_count = COALESCE(%s, total_count),
                processed_count = COALESCE(%s, processed_count),
                current_item = COALESCE(%s, current_item),
                error_text = COALESCE(%s, error_text),
                metadata_json = COALESCE(%s, metadata_json),
                updated_at = now()
                {completed}
            WHERE id = %s
            """,
            (status, phase, total_count, processed_count, current_item, error_text, _json(metadata) if metadata is not None else None, job_id),
        )
        self._commit()

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT %s", (limit,)))

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        return self._execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()

    def get_running_sync_job_for_account(self, account_id: int) -> dict[str, Any] | None:
        return self._execute(
            """
            SELECT *
            FROM jobs
            WHERE job_type = 'sync'
              AND status = 'running'
              AND metadata_json->>'account_id' = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT 1
            """,
            (str(account_id),),
        ).fetchone()

    def get_clustering_status_counts(
        self,
        embedding_model: str,
        algorithm_version: str = "recent-centroid-v1",
    ) -> dict[str, int]:
        row = self._execute(
            """
            SELECT
              COUNT(*) AS ready_embeddings,
              COUNT(*) FILTER (
                WHERE work.status = 'done'
                  AND work.source_hash = me.input_hash
                  AND work.algorithm_version = %s
                  AND membership.message_id IS NOT NULL
              ) AS clustered_messages
            FROM message_embeddings me
            JOIN message_processing_state ps ON ps.message_id = me.message_id
            LEFT JOIN message_clustering_work work
              ON work.message_id = me.message_id
             AND work.embedding_model = me.embedding_model
            LEFT JOIN (
              SELECT DISTINCT mtm.message_id
              FROM message_topic_memberships mtm
              JOIN topic_clusters tc ON tc.id = mtm.topic_cluster_id
              WHERE tc.embedding_model = %s
            ) membership ON membership.message_id = me.message_id
            WHERE me.status = 'ready'
              AND me.embedding_model = %s
              AND ps.summary_status = 'done'
              AND ps.embedding_status = 'done'
              AND ps.embedding_model = me.embedding_model
            """,
            (algorithm_version, embedding_model, embedding_model),
        ).fetchone()
        if row is None:
            return {
                "ready_embeddings": 0,
                "clustered_messages": 0,
            }
        return {
            "ready_embeddings": int(row["ready_embeddings"] or 0),
            "clustered_messages": int(row["clustered_messages"] or 0),
        }

    def mark_interrupted_jobs(self, started_before: datetime) -> int:
        cursor = self._execute(
            """
            UPDATE jobs
            SET status = 'failed',
                phase = 'failed',
                error_text = CASE
                  WHEN error_text = '' THEN 'Job was interrupted before this app process started.'
                  ELSE error_text
                END,
                completed_at = COALESCE(completed_at, now()),
                updated_at = now()
            WHERE status = 'running' AND started_at < %s
            """,
            (started_before,),
        )
        self._commit()
        return cursor.rowcount


    def expire_stale_running_jobs(self, updated_before: datetime) -> int:
        cursor = self._execute(
            """
            UPDATE jobs
            SET status = 'failed',
                phase = 'failed',
                error_text = CASE
                  WHEN error_text = '' THEN 'Job did not report progress before the stale-job timeout.'
                  ELSE error_text
                END,
                completed_at = COALESCE(completed_at, now()),
                updated_at = now()
            WHERE status = 'running' AND updated_at < %s
            """,
            (updated_before,),
        )
        self._commit()
        return cursor.rowcount

    def schedule_account_mailboxes(self, account_id: int) -> int:
        cursor = self._execute(
            """
            UPDATE mailboxes
            SET next_sync_at = now(),
                crawl_last_error = '',
                crawl_status = CASE WHEN crawl_status = 'running' THEN crawl_status ELSE 'idle' END
            WHERE account_id = %s AND monitored = true
            """,
            (account_id,),
        )
        self._commit()
        return cursor.rowcount


    def release_mailbox_claim(
        self,
        mailbox_id: int,
        *,
        status: str,
        error_text: str = "",
        next_sync_at: datetime | None = None,
        clear_next_sync_at: bool = False,
    ) -> None:
        self._execute(
            """
            UPDATE mailboxes
            SET crawl_status = %s,
                crawl_lease_expires_at = NULL,
                crawl_last_error = %s,
                crawl_attempt_count = CASE WHEN %s = 'completed' THEN 0 ELSE crawl_attempt_count END,
                next_sync_at = CASE WHEN %s THEN NULL ELSE COALESCE(%s, next_sync_at) END,
                last_crawled_at = CASE WHEN %s = 'completed' THEN now() ELSE last_crawled_at END,
                last_synced_at = CASE WHEN %s = 'completed' THEN now() ELSE last_synced_at END
            WHERE id = %s
            """,
            (status, error_text, status, clear_next_sync_at, next_sync_at, status, status, mailbox_id),
        )
        self._commit()

    def list_stage_preset_health(self, stage: str, canonical_embedding_model: str = "") -> list[dict[str, Any]]:
        params: list[object] = [stage]
        filter_sql = ""
        if stage == "embedding" and canonical_embedding_model:
            filter_sql = "AND preset.embedding_model = %s"
            params.append(canonical_embedding_model)
        return list(
            self._execute(
                f"""
                SELECT
                  preset.*,
                  (health.preset_id IS NULL) AS health_unchecked,
                  COALESCE(health.status, 'healthy') AS health_status,
                  COALESCE(health.consecutive_failures, 0) AS consecutive_failures,
                  health.last_failure_at,
                  health.last_success_at,
                  health.cooldown_until,
                  health.updated_at AS health_updated_at,
                  COALESCE(health.last_error_text, '') AS last_error_text
                FROM llm_presets preset
                LEFT JOIN llm_preset_stage_health health
                  ON health.preset_id = preset.id AND health.stage = %s
                WHERE preset.enabled = true
                {filter_sql}
                ORDER BY preset.priority, preset.id
                """,
                tuple(params),
            )
        )

    def mark_preset_stage_success(self, preset_id: int, stage: str) -> None:
        self._execute(
            """
            INSERT INTO llm_preset_stage_health (
              preset_id, stage, status, consecutive_failures, last_failure_at,
              last_success_at, cooldown_until, last_error_text, updated_at
            )
            VALUES (%s, %s, 'healthy', 0, NULL, now(), NULL, '', now())
            ON CONFLICT (preset_id, stage) DO UPDATE SET
              status = 'healthy',
              consecutive_failures = 0,
              last_success_at = now(),
              cooldown_until = NULL,
              last_error_text = '',
              updated_at = now()
            """,
            (preset_id, stage),
        )
        self._commit()

    def mark_preset_stage_probing(self, preset_id: int, stage: str) -> None:
        self._execute(
            """
            INSERT INTO llm_preset_stage_health (preset_id, stage, status, updated_at)
            VALUES (%s, %s, 'probing', now())
            ON CONFLICT (preset_id, stage) DO UPDATE SET
              status = 'probing',
              updated_at = now()
            """,
            (preset_id, stage),
        )
        self._commit()

    def mark_preset_stage_failure(self, preset_id: int, stage: str, error_text: str, cooldown_seconds: int) -> None:
        row = self._execute(
            """
            SELECT consecutive_failures
            FROM llm_preset_stage_health
            WHERE preset_id = %s AND stage = %s
            """,
            (preset_id, stage),
        ).fetchone()
        failures = int(row["consecutive_failures"] or 0) + 1 if row else 1
        cooldown_until = datetime.now(UTC) + timedelta(seconds=cooldown_seconds)
        self._execute(
            """
            INSERT INTO llm_preset_stage_health (
              preset_id, stage, status, consecutive_failures, last_failure_at,
              cooldown_until, last_error_text, updated_at
            )
            VALUES (%s, %s, 'cooldown', %s, now(), %s, %s, now())
            ON CONFLICT (preset_id, stage) DO UPDATE SET
              status = 'cooldown',
              consecutive_failures = %s,
              last_failure_at = now(),
              cooldown_until = %s,
              last_error_text = %s,
              updated_at = now()
            """,
            (
                preset_id,
                stage,
                failures,
                cooldown_until,
                error_text[:500],
                failures,
                cooldown_until,
                error_text[:500],
            ),
        )
        self._commit()

    def list_due_preset_cooldown_recoveries(self, stage: str, canonical_embedding_model: str = "") -> list[dict[str, Any]]:
        params: list[object] = [stage]
        filter_sql = ""
        if stage == "embedding" and canonical_embedding_model:
            filter_sql = "AND preset.embedding_model = %s"
            params.append(canonical_embedding_model)
        return list(
            self._execute(
                f"""
                SELECT preset.*, health.stage, health.status AS health_status, health.cooldown_until,
                       health.consecutive_failures, health.last_error_text
                FROM llm_preset_stage_health health
                JOIN llm_presets preset ON preset.id = health.preset_id
                WHERE health.stage = %s
                  AND preset.enabled = true
                  AND health.status IN ('cooldown', 'probing')
                  AND (health.cooldown_until IS NULL OR health.cooldown_until <= now())
                  {filter_sql}
                ORDER BY preset.priority, preset.id
                """,
                tuple(params),
            )
        )


    def get_analysis_message_detail(self, message_id: int) -> dict[str, Any] | None:
        return self._execute(
            """
            SELECT
              m.*,
              bc.body_text,
              SUBSTRING(COALESCE(bc.body_text, m.cleaned_text, '') FROM 1 FOR 1500) AS preview_text,
              s.summary_text,
              s.importance_score,
              s.rationale,
              s.confidence,
              s.model_name,
              box.account_id,
              box.provider_mailbox_id,
              box.name AS mailbox_name
            FROM messages m
            JOIN mailboxes box ON box.id = m.mailbox_id
            LEFT JOIN message_body_cache bc ON bc.message_id = m.id
            LEFT JOIN message_summaries s ON s.message_id = m.id
            WHERE m.id = %s
            """,
            (message_id,),
        ).fetchone()

    def complete_analysis(
        self,
        message_id: int,
        *,
        interest_input_hash: str,
        summary_input_hash: str,
        preset_name: str,
        summary_needs_refresh: bool,
    ) -> None:
        self._execute(
            """
            UPDATE message_processing_state
            SET interest_status = 'done',
                interest_input_hash = %s,
                interest_lease_expires_at = NULL,
                summary_status = 'done',
                summary_input_hash = %s,
                summary_lease_expires_at = NULL,
                summary_needs_refresh = %s,
                last_summary_preset_name = %s,
                updated_at = now()
            WHERE message_id = %s
            """,
            (_hash_text(interest_input_hash), _hash_text(summary_input_hash), summary_needs_refresh, preset_name, message_id),
        )
        self._commit()


    def defer_analysis(self, message_id: int, error_text: str, retry_seconds: int) -> None:
        self._execute(
            """
            UPDATE message_processing_state
            SET interest_status = 'running',
                summary_status = 'running',
                interest_lease_expires_at = now() + (%s * interval '1 second'),
                summary_lease_expires_at = now() + (%s * interval '1 second'),
                interest_last_error = %s,
                summary_last_error = %s,
                updated_at = now()
            WHERE message_id = %s
            """,
            (retry_seconds, retry_seconds, error_text[:500], error_text[:500], message_id),
        )
        self._commit()


    def complete_embedding(self, message_id: int, *, input_text: str, embedding_model: str, preset_name: str) -> None:
        self._execute(
            """
            UPDATE message_processing_state
            SET embedding_status = 'done',
                embedding_input_hash = %s,
                embedding_model = %s,
                embedding_lease_expires_at = NULL,
                embedding_needs_refresh = false,
                last_embedding_preset_name = %s,
                updated_at = now()
            WHERE message_id = %s
            """,
            (_hash_text(input_text), embedding_model, preset_name, message_id),
        )
        self._commit()


    def defer_embedding(self, message_id: int, error_text: str, retry_seconds: int) -> None:
        self._execute(
            """
            UPDATE message_processing_state
            SET embedding_status = 'running',
                embedding_lease_expires_at = now() + (%s * interval '1 second'),
                embedding_last_error = %s,
                updated_at = now()
            WHERE message_id = %s
            """,
            (retry_seconds, error_text[:500], message_id),
        )
        self._commit()


    def get_interval_summary_embedding_detail(self, summary_id: int) -> dict[str, Any] | None:
        return self._execute(
            """
            SELECT s.*, b.name AS mailbox_name, b.provider_mailbox_id
            FROM mailbox_interval_summaries s
            JOIN mailboxes b ON b.id = s.mailbox_id
            WHERE s.id = %s
            """,
            (summary_id,),
        ).fetchone()

    def complete_interval_summary_embedding(
        self, summary_id: int, *, input_text: str, embedding_model: str, preset_name: str
    ) -> None:
        self._execute(
            """
            UPDATE mailbox_interval_summaries
            SET embedding_status = 'done',
                embedding_input_hash = %s,
                embedding_model = %s,
                embedding_lease_expires_at = NULL,
                embedding_needs_refresh = false,
                last_embedding_preset_name = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (_hash_text(input_text), embedding_model, preset_name, summary_id),
        )
        self._commit()


    def defer_interval_summary_embedding(self, summary_id: int, error_text: str, retry_seconds: int) -> None:
        self._execute(
            """
            UPDATE mailbox_interval_summaries
            SET embedding_status = 'running',
                embedding_lease_expires_at = now() + (%s * interval '1 second'),
                embedding_last_error = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (retry_seconds, error_text[:500], summary_id),
        )
        self._commit()

    def mark_embedding_refresh_pending(self, embedding_model: str) -> int:
        row = self._execute(
            """
            WITH affected AS (
              UPDATE message_processing_state
              SET embedding_status = 'pending',
                  embedding_needs_refresh = true,
                  updated_at = now()
              WHERE summary_status = 'done'
                AND (embedding_model = %s OR embedding_model = '' OR embedding_status = 'done')
              RETURNING message_id
            ),
            deleted_embeddings AS (
              DELETE FROM message_embeddings me
              USING affected
              WHERE me.message_id = affected.message_id
                AND me.embedding_model = %s
              RETURNING me.message_id
            )
            SELECT COUNT(*) AS marked_messages FROM affected
            """,
            (embedding_model, embedding_model),
        ).fetchone()
        self._commit()
        return int(row["marked_messages"] or 0) if row else 0

    def mark_interval_summary_embedding_refresh_pending(self, embedding_model: str) -> int:
        entries_text = interval_summary_entries_text_sql("mailbox_interval_summaries")
        row = self._execute(
            f"""
            WITH affected AS (
              UPDATE mailbox_interval_summaries
              SET embedding_status = 'pending',
                  embedding_needs_refresh = true,
                  updated_at = now()
              WHERE refreshed_at IS NOT NULL
                AND {entries_text} <> ''
                AND (embedding_model = %s OR embedding_model = '' OR embedding_status = 'done')
              RETURNING id
            ),
            deleted_embeddings AS (
              DELETE FROM interval_summary_embeddings ise
              USING affected
              WHERE ise.summary_id = affected.id
                AND ise.embedding_model = %s
              RETURNING ise.summary_id
            )
            SELECT COUNT(*) AS marked_summaries FROM affected
            """,
            (embedding_model, embedding_model),
        ).fetchone()
        self._commit()
        return int(row["marked_summaries"] or 0) if row else 0

    def reset_topic_rebuild_state(self, embedding_model: str) -> dict[str, int]:
        row = self._execute(
            """
            WITH ready AS (
              SELECT COUNT(*) AS ready_embeddings
              FROM message_embeddings me
              JOIN message_processing_state ps ON ps.message_id = me.message_id
              WHERE me.embedding_model = %s
                AND me.status = 'ready'
                AND ps.summary_status = 'done'
                AND ps.embedding_status = 'done'
                AND ps.embedding_model = me.embedding_model
            ),
            removed_memberships AS (
              DELETE FROM message_topic_memberships mtm
              USING topic_clusters tc
              WHERE mtm.topic_cluster_id = tc.id
                AND tc.embedding_model = %s
              RETURNING mtm.message_id
            ),
            removed_clusters AS (
              DELETE FROM topic_clusters
              WHERE embedding_model = %s
              RETURNING id
            ),
            upsert_state AS (
              INSERT INTO cluster_rebuild_state (
                embedding_model, status, dirty_since, lease_expires_at, last_error_text,
                last_ready_embedding_count, updated_at
              )
              VALUES (%s, 'dirty', now(), NULL, '', 0, now())
              ON CONFLICT (embedding_model) DO UPDATE SET
                status = 'dirty',
                dirty_since = now(),
                lease_expires_at = NULL,
                last_error_text = '',
                last_ready_embedding_count = 0,
                updated_at = now()
              RETURNING embedding_model
            )
            SELECT
              (SELECT ready_embeddings FROM ready) AS ready_embeddings,
              0 AS stale_embeddings,
              (SELECT COUNT(*) FROM removed_memberships) AS topic_memberships,
              (SELECT COUNT(*) FROM removed_clusters) AS topic_clusters
            """,
            (embedding_model, embedding_model, embedding_model, embedding_model),
        ).fetchone()
        self._commit()
        return {
            "ready_embeddings": int(row["ready_embeddings"] or 0) if row else 0,
            "stale_embeddings": int(row["stale_embeddings"] or 0) if row else 0,
            "topic_memberships": int(row["topic_memberships"] or 0) if row else 0,
            "topic_clusters": int(row["topic_clusters"] or 0) if row else 0,
        }






    def get_pipeline_status_counts(self, embedding_model: str) -> dict[str, dict[str, int]]:
        # Artifacts describe completion; only the runnable queue describes execution.
        groups = {row["kind"]: row for row in self._execute("""
            SELECT kind, count(*) FILTER (WHERE state='processing') AS running,
              count(*) FILTER (WHERE state='new') AS queued,
              count(*) FILTER (WHERE state='new' AND retries > 0) AS retrying
            FROM work_queue WHERE kind NOT IN ('embed_message','embed_interval')
              OR payload->>'embedding_model'=%s GROUP BY kind
        """, (embedding_model,)).fetchall()}

        def counts(row, kinds):
            result = {key: int(row.get(key) or 0) for key in ('total', 'done', 'waiting_for_analysis')}
            for key in ('running', 'queued', 'retrying'):
                result[key] = sum(int(groups.get(kind, {}).get(key) or 0) for kind in kinds)
            result['claimable'] = result['queued']
            return result

        messages = self._execute("""SELECT count(*) AS total,
            count(*) FILTER (WHERE summary_status='done' AND interest_status='done'
              AND NOT summary_needs_refresh) AS analyzed_done,
            count(*) FILTER (WHERE summary_status='done' AND NOT summary_needs_refresh
              AND embedding_status='done' AND NOT embedding_needs_refresh AND embedding_model=%s) AS done,
            count(*) FILTER (WHERE summary_status<>'done' OR summary_needs_refresh) AS waiting_for_analysis
            FROM message_processing_state""", (embedding_model,)).fetchone()
        intervals = self._execute("""WITH inputs AS MATERIALIZED (
            SELECT refreshed_at, embedding_status, embedding_needs_refresh, embedding_model,
              EXISTS (SELECT 1 FROM jsonb_array_elements(
                COALESCE(s.summary_json->'entries', '[]'::jsonb)) AS entry
                WHERE COALESCE(entry->>'summary', '') <> '') AS has_entries
            FROM mailbox_interval_summaries s
            ) SELECT
            count(*) FILTER (WHERE refreshed_at IS NOT NULL AND has_entries) AS total,
            count(*) FILTER (WHERE refreshed_at IS NOT NULL AND has_entries
              AND embedding_status='done' AND NOT embedding_needs_refresh AND embedding_model=%s) AS done,
            count(*) FILTER (WHERE refreshed_at IS NULL OR NOT has_entries) AS waiting_for_analysis
            FROM inputs""", (embedding_model,)).fetchone()
        summaries = self._execute("""SELECT count(*) AS total,
            count(*) FILTER (WHERE refreshed_at IS NOT NULL AND request_rebuild IS NULL
              AND NOT EXISTS (SELECT 1 FROM work_queue q
                WHERE q.dedup_key='interval:'||s.id::text)) AS done
            FROM mailbox_interval_summaries s""").fetchone()
        message_counts = counts(messages, ['embed_message'])
        interval_counts = counts(intervals, ['embed_interval'])
        return {
            'analyses': counts({'total': messages['total'], 'done': messages['analyzed_done']}, ['analyze_message']),
            'embeddings': {key: message_counts[key]+interval_counts[key] for key in message_counts},
            'message_embeddings': message_counts,
            'interval_summary_embeddings': interval_counts,
            'summaries': counts(summaries, ['interval_daily','interval_rollup']),
        }

    def get_cluster_pipeline_status(self, embedding_model: str,
                                    algorithm_version: str = 'recent-centroid-v1') -> dict[str, int | str]:
        counts = self.get_clustering_status_counts(embedding_model, algorithm_version)
        row = self._execute("""SELECT
            count(*) FILTER (WHERE state='processing') AS running,
            count(*) FILTER (WHERE state='new') AS queued,
            count(*) FILTER (WHERE state='new' AND retries>0) AS retrying
            FROM work_queue WHERE kind IN ('cluster_message','label_cluster','cluster_publish')
              AND payload->>'embedding_model'=%s""", (embedding_model,)).fetchone()
        return {**counts, **{key: int(row[key] or 0) for key in ('running','queued','retrying')},
                'status': 'running' if row['running'] else 'queued' if row['queued'] else 'idle'}

    def list_pipeline_mailbox_statuses(self, embedding_model: str) -> list[dict[str, Any]]:
        return list(
            self._execute(
                """
                SELECT
                  box.id AS mailbox_id,
                  box.account_id,
                  account.name AS account_name,
                  account.provider_type,
                  account.auth_mode,
                  account.status AS account_status,
                  box.name AS mailbox_name,
                  box.provider_mailbox_id,
                  box.monitored,
                  box.last_synced_at,
                  box.crawl_status AS sync_status,
                  box.crawl_last_error,
                  COALESCE(crawl_job.total_count, 0) AS crawl_total_count,
                  COALESCE(crawl_job.processed_count, 0) AS crawl_processed_count,
                  COALESCE(crawl_job.status, box.crawl_status, 'idle') AS crawl_job_status,
                  COALESCE(message_counts.total_messages, 0) AS total_messages,
                  COALESCE(message_counts.analyzed_done, 0) AS analyzed_done,
                  COALESCE(message_counts.embedded_done, 0) AS embedded_done
                FROM mailboxes box
                JOIN mail_accounts account ON account.id = box.account_id
                LEFT JOIN (
                  SELECT m.mailbox_id, COUNT(*) AS total_messages,
                    COUNT(*) FILTER (WHERE ps.summary_status = 'done'
                      AND NOT ps.summary_needs_refresh) AS analyzed_done,
                    COUNT(*) FILTER (WHERE ps.summary_status = 'done'
                      AND ps.embedding_status = 'done' AND ps.embedding_model = %s
                      AND NOT ps.summary_needs_refresh
                      AND NOT ps.embedding_needs_refresh) AS embedded_done
                  FROM messages m
                  LEFT JOIN message_processing_state ps ON ps.message_id = m.id
                  GROUP BY m.mailbox_id
                ) message_counts ON message_counts.mailbox_id = box.id
                LEFT JOIN LATERAL (
                  SELECT j.status, j.total_count, j.processed_count
                  FROM jobs j
                  WHERE j.job_type = 'crawl'
                    AND j.status = 'running'
                    AND j.metadata_json->>'mailbox_id' = box.id::text
                  ORDER BY j.updated_at DESC, j.id DESC
                  LIMIT 1
                ) crawl_job ON true
                ORDER BY account.name, box.name
                """,
                (embedding_model,),
            )
        )
