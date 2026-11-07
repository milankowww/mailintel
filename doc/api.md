<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# HTTP API

Use the API to integrate your own tools with a private MailIntel installation. The running app exposes interactive documentation at `/docs` and the generated OpenAPI schema at `/openapi.json`; these describe the exact request and response fields for the running version.

Most `/api` routes require `Authorization: Bearer <token>`. Retrieve the token using the [operations guide](operations.md#authentication-and-access). Health is available at `GET /api/health`.

```sh
# Set MAILINTEL_TOKEN to your installation's token without committing it.
curl -H "Authorization: Bearer $MAILINTEL_TOKEN" http://127.0.0.1:8000/api/accounts
```

The API covers account and mailbox configuration, triage profiles, model presets, pipeline settings, jobs, maintenance, message retrieval, stars, topics, and asynchronous smart search. Use the documented job endpoints to poll progress and cancel work where supported. Maintenance and delete routes modify local data; inspect their schemas before calling them.

A token grants access to the whole installation. Keep it private when sharing URLs, source, screenshots, or issue reports. OAuth callback routes validate consent state for the account connection flow.

For implementation details, route modules live under `backend/mailintel/api/routers/` and request models under `backend/mailintel/api/schemas/`.

## Offline synchronization

`GET /api/sync?after=0&limit=500` downloads current reading data for an offline client.
Start with `after=0`, then use the returned `generation` for the next page while
`has_more` is true. The default page size is 500 rows, with a maximum of 2,000;
rows sharing the boundary generation are included together.

The response contains `generation`, `has_more`, and `tables`. Each table holds an
array of rows with their original primary keys and `sync_generation`. The tables
are `mailboxes`, `messages`, `message_summaries`, `mailbox_interval_summaries`,
`interest_profiles`, `topic_clusters`, `message_body_cache`, and `smart_search_jobs`.
Messages also contain complete `interest_matches` and `topic_memberships` arrays;
replace these relationships when applying a message, including empty arrays.
Mailbox rows include `account_name` for display. Fields are explicitly selected
for reading; credentials, embeddings and internal worker state stay on the server.

Save each page's rows and cursor together in one local database transaction.
An interrupted download can safely repeat the same request. Changes to an existing
row give it a higher generation, so subsequent downloads refresh it. Rows from
different tables may arrive on different pages; retain their IDs while related
records are downloading. Bodies already cached by the server are included; the
message's `cleaned_text` provides its stored text when no separate body is cached.

After catching up, `GET /api/sync/ids` returns `generation` and a `tables` map of
current primary-key lists. Remove local records absent from those lists, limited
to local rows at or below the manifest's generation. This reconciles deletions
without retaining a deletion log. Repeat periodically, including after reconnecting.
The manifest's generation is a reconciliation boundary, **not** a replacement for
the download cursor: changes committed since the last page still need downloading.

A cursor ahead of the server returns HTTP 409 and requires a fresh local download.
After replacing or restoring the server database, clear client downloads and sync
from zero. This also handles restorations whose generation happens to exceed an
older client's cursor. New searches and live processing status use their existing
API endpoints.

### Message address details

`GET /api/messages/{message_id}` returns the message and its summary in `message`.
Address fields are `sender_name`, `sender_address`, `recipients_json` (To), and
`cc_json` (Cc). Recipient fields are arrays of strings, containing either an
address or a display name with an address, such as `Alice <alice@example.com>`.
An empty array means no recipients are listed in that field. Older EWS records
may contain empty strings until those messages are fetched again.
