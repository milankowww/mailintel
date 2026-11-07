<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# Architecture

MailIntel runs a FastAPI application, a Vue/Vuetify frontend, and background processing threads against PostgreSQL with pgvector. One application process owns the worker runtime. The model service is a separate dependency selected by the operator.

## Data flow

1. Read-only IMAP or EWS connectors fetch messages from selected mailboxes.
2. Cleaning normalizes message bodies; PostgreSQL stores message metadata and cached text.
3. Model workers generate summaries and embeddings. Interest matching and incremental clustering derive dashboard views.
4. Interval jobs build mailbox summaries. Search jobs retrieve indexed evidence and use configured models to plan, compress, and answer.
5. The browser reads API views and submits configuration or processing requests.

Daily, weekly, and monthly summaries include links to contributing messages. Each model response is checked against the IDs supplied in that input batch; merge passes check their temporary entry IDs before translating them back to message IDs. Message IDs must be positive integers; digit-only strings are converted to integers before validation. A reference outside the supplied input rejects the response and retries the job through the work queue. Inputs are JSON records, separate from the instructions. Every call uses compact record IDs, checked and translated back to the original message IDs locally. Weekly/monthly inputs and merge inputs retain each source entry’s message links as a unit. Batching splits record lists, preserving quotes and newlines within fields. Before storing an interval summary, any input message IDs omitted from the generated groups are collected in a final “Other messages” entry, including omissions during merging.

Mail content is untrusted input, including instructions an attacker might embed in a message. Model calls are constrained application operations scoped to analysis and retrieval. Mailbox credentials stay with the connectors, and model outputs become derived data in the application database. This keeps AI mistakes confined to analysis: sending, deleting, or modifying mail is outside the model’s authority. Users review the results and decide what action to take.

The backend accesses configured mail and model servers. Operators can choose cloud-based frontier models, sharing the submitted email content with their provider, or small local models that keep inference within their own infrastructure.

## Storage

`mail_accounts` and `mailboxes` hold connection configuration and synchronization state. `messages`, `message_body_cache`, `message_summaries`, and `message_embeddings` hold ingested and derived message data. PostgreSQL full-text indexes and pgvector support retrieval.

`interest_profiles`, `message_interest_matches`, `topic_clusters`, and `message_topic_memberships` provide personalized and discovered groupings. `mailbox_interval_summaries` and `interval_summary_embeddings` support summaries over time. `smart_search_jobs` and `smart_search_job_messages` retain search progress and evidence associations.

`work_queue` holds durable work, deduplication keys, priorities, state, and revision-based ownership. `queue_workers` and `queue_events` expose processing status. Preset workers respect model concurrency and capabilities. Local workers handle retrieval and clustering. Restart recovery returns interrupted work to the queue; revision checks prevent stale workers from publishing superseded results.

## Source map

| Path | Responsibility |
| --- | --- |
| `backend/mailintel/api/` | HTTP routes, validation, serialization |
| `backend/mailintel/app/` | Application creation and lifecycle |
| `backend/mailintel/connectors/` | Read-only mail protocols |
| `backend/mailintel/services/` | Analysis, retrieval, clustering, queue orchestration |
| `backend/mailintel/persistence/` | SQL access, schema, and Alembic revision |
| `backend/mailintel/resources/` | Prompts, cleaning rules, OAuth bridge |
| `frontend/src/` | Browser application |
| `tests/backend/`, `frontend/tests/` | Automated checks |

The schema initializer in `persistence/orm.py` creates the current schema and applies the additive synchronization-generation upgrade on existing installations. Alembic revisions retain frozen SQL independently of the current models. See [Contributing](../CONTRIBUTING.md) before changing either.

Offline clients download selected reading tables through the [sync API](api.md#offline-synchronization). Indexed `sync_generation` columns record each row's latest change. Database triggers allocate generations from one transactional counter, including changes made by background workers and relationship replacements. Writers acquire the counter before synchronized row locks; model and network calls run outside these write transactions. Each download uses one database snapshot and a shared cursor across tables. Clients save rows and cursor atomically, and periodically reconcile the server's current ID lists to remove deleted records. No per-client registry or change-history table is maintained.
