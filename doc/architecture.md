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

The schema initializer in `persistence/orm.py` creates the current schema. Alembic's initial SQL is frozen independently to keep the revision stable as the models evolve. See [Contributing](../CONTRIBUTING.md) before changing either.
