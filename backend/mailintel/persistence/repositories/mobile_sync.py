# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

"""Current-state downloads for offline readers; all queries use one MVCC snapshot."""

# Explicit columns keep credentials, provider internals and embeddings server-side.
SYNC_COLUMNS = {
    "mailboxes": "id account_id name monitored",
    "messages": "id mailbox_id subject sender_name sender_address recipients_json cc_json received_at sent_at star is_read has_attachments cleaned_text",
    "message_summaries": "message_id summary_text importance_score rationale confidence model_name summarized_at updated_at",
    "mailbox_interval_summaries": "id mailbox_id summary_type interval_start_date period_days refreshed_at request_rebuild summary_json",
    "interest_profiles": "id name description collaborators_json signals priority_weight dashboard_prominence summary_detail watch_mode",
    "topic_clusters": "id name description key_terms_json key_entities_json llm_summary why_it_matters importance_score",
    "message_body_cache": "message_id body_text source_segment",
    "smart_search_jobs": "job_id question status safe_error_text result_json coverage_json matching_sources_json created_at updated_at completed_at",
}
SYNC_KEYS = {table: columns.split()[0] for table, columns in SYNC_COLUMNS.items()}


def _row_projection(table):
    columns = [f"r.{column}" for column in SYNC_COLUMNS[table].split()]
    columns.append("r.sync_generation")
    if table == "mailboxes":
        columns.append("(SELECT name FROM mail_accounts WHERE id = r.account_id) AS account_name")
    if table == "messages":
        columns += [
            """COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'interest_profile_id', x.interest_profile_id, 'score', x.score,
                'rationale', x.rationale, 'primary_match', x.primary_match)
                ORDER BY x.interest_profile_id) FROM message_interest_matches x
                WHERE x.message_id = r.id), '[]'::jsonb) AS interest_matches""",
            """COALESCE((SELECT jsonb_agg(jsonb_build_object(
                'topic_cluster_id', x.topic_cluster_id, 'score', x.score,
                'is_primary', x.is_primary) ORDER BY x.topic_cluster_id)
                FROM message_topic_memberships x WHERE x.message_id = r.id),
                '[]'::jsonb) AS topic_memberships""",
        ]
    return ", ".join(columns)


class MobileSyncRepository:
    def mobile_sync_page(self, after: int, limit: int):
        # Each branch can use its generation index. The cutoff is global and includes
        # ties, so advancing the cursor cannot skip rows from another table.
        branches = " UNION ALL ".join(
            f"(SELECT sync_generation FROM {table} WHERE sync_generation > %(after)s "
            "ORDER BY sync_generation LIMIT %(branch_limit)s)" for table in SYNC_COLUMNS
        )
        tables = []
        more = " OR ".join(
            f"EXISTS(SELECT 1 FROM {table} WHERE sync_generation > cutoff.value)"
            for table in SYNC_COLUMNS
        )
        for table in SYNC_COLUMNS:
            tables += [f"'{table}'", f"""COALESCE((SELECT jsonb_agg(to_jsonb(item)
                ORDER BY item.sync_generation, item.{SYNC_KEYS[table]}) FROM (
                SELECT {_row_projection(table)} FROM {table} r
                WHERE r.sync_generation > %(after)s AND r.sync_generation <= cutoff.value
                ) item), '[]'::jsonb)"""]
        return self._execute(f"""
            WITH current AS (SELECT generation FROM sync_counter WHERE id = 1),
            candidates AS ({branches}),
            page AS (SELECT sync_generation FROM candidates ORDER BY sync_generation LIMIT %(limit)s),
            cutoff AS (SELECT COALESCE(max(sync_generation), (SELECT generation FROM current)) AS value FROM page)
            SELECT jsonb_build_object(
                'generation', cutoff.value,
                'has_more', ({more}),
                'reset_required', %(after)s > current.generation,
                'tables', jsonb_build_object({', '.join(tables)})
            ) AS payload FROM current, cutoff
        """, {"after": after, "limit": limit, "branch_limit": limit + 1}).fetchone()["payload"]

    def mobile_sync_ids(self):
        tables = []
        for table, key in SYNC_KEYS.items():
            tables += [f"'{table}'", f"COALESCE((SELECT jsonb_agg({key} ORDER BY {key}) FROM {table}), '[]'::jsonb)"]
        return self._execute(f"""SELECT jsonb_build_object(
            'generation', generation, 'tables', jsonb_build_object({', '.join(tables)})
            ) AS payload FROM sync_counter WHERE id = 1""").fetchone()["payload"]
