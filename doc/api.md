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
