<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# Installation

## Docker

Install Docker Engine or Docker Desktop with the Compose plugin. Start with the commands in the [README](../README.md#quick-start). All Compose commands in this repository use `--project-directory .` and run from the repository root so build contexts, `.env`, and persistent paths resolve correctly.

Choose an alphanumeric database password for the simplest setup: Compose interpolates it into a PostgreSQL connection URL. Other characters require URL encoding in that URL. Keep `.env` private.

```sh
docker compose --project-directory . -f deploy/compose/docker-compose.yml logs --tail=100 app
docker compose --project-directory . -f deploy/compose/docker-compose.yml down
```

`down` stops the services; the bind-mounted `docker-data/` directory remains. Avoid running the development and normal Compose stacks against the same database directory simultaneously.

## Connect a model service

Choose between cloud-based frontier models and local inference. Cloud providers receive the email content submitted for processing. A small local model can fit in just a few gigabytes of memory; actual requirements depend on the model, quantization, context size, and concurrency. Choose a model that supports MailIntel’s analysis tasks and fits your available resources.

MailIntel supports Ollama and OpenAI-compatible endpoints. Install or run your chosen provider separately and load a text-generation model and an embedding model. In LLM Settings, enter its endpoint, authentication, model names, context sizes, output budget, and supported task capabilities. Use the connectivity check before enabling processing. The text model must support the structured output expected by the analysis and search prompts.

For a provider on the Docker host, use `http://host.docker.internal:11434` for Ollama. The Compose files map this name to the host gateway on Linux. The provider must listen on an address reachable from containers; a service bound only to host loopback may need configuration. Restrict provider access to trusted clients. `localhost` inside the app container refers to that container.

For an OpenAI-compatible provider, enter the API base URL it exposes, commonly ending in `/v1`. Use the provider's exact model identifiers. Configure credentials through the preset's supported secret mode. Remote inference sends mail content to that provider.

Choose a canonical embedding model and keep it consistent across message indexing and search. Changing it requires rebuilding embeddings in Maintenance. Enable search planning, embedding, compression, and answer capabilities on suitable presets. Preset priority controls routing, and these tasks can share a suitable provider.

## Local Python

Requirements: Python 3.11 or newer, Node.js 22.12 or newer for the frontend build, and PostgreSQL 16 with pgvector installed. Create a database and role with permission to create the `vector` extension (or ask your database administrator to install the extension first).

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e 'backend[dev]'
export MAILINTEL_DATABASE_URL='postgresql://mailintel:password@127.0.0.1:5432/mailintel'
(cd frontend && npm ci && npm run build)
mailintel init-db
mailintel auth token
mailintel serve --reload
```

For the Python app, export settings in your shell. Local secrets default to `~/.config/mailintel`. Open http://127.0.0.1:8000. The backend serves the frontend and its bundled assets.

For the pinned Python 3.12 release environment and package verification, follow [Building and releasing MailIntel](releases.md).

## Development with Docker

Build the frontend with `npm ci && npm run build` from `frontend/`, then run:

```sh
docker compose --project-directory . -f deploy/compose/docker-compose.dev.yml up
```

This installs the Python package in editable mode and reloads backend code. Rebuild the frontend after frontend edits. Development Compose publishes PostgreSQL on localhost port 5432; change `MAILINTEL_DB_PORT` if that port is occupied. See [Contributing](../CONTRIBUTING.md) for tests.
