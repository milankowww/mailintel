# MailIntel

MailIntel helps you find important messages, understand recurring topics, and ask questions about your email. It combines read-only mailbox ingestion with AI summaries, priority triage, interest profiles, and search with message sources.

Your mail stays in your own PostgreSQL database. You choose the model provider: a local service for private processing, or a remote provider that receives the content needed for analysis. Models receive analysis inputs through a constrained interface; mailbox credentials stay with the connectors. AI answers can be wrong; check their source messages before acting.

## Quick start

You need Docker with Compose, access to an IMAP or Exchange EWS mailbox, and a reachable model service providing text generation and embeddings. Run these commands from the repository root:

```sh
cp .env.example .env
# Edit .env and replace POSTGRES_PASSWORD with a unique password.
docker compose --project-directory . -f deploy/compose/docker-compose.yml up -d --build
docker compose --project-directory . -f deploy/compose/docker-compose.yml exec app mailintel auth token
```

Open **http://127.0.0.1:8000** and enter the printed token. Keep it private: it grants access to your mail and settings.

1. In **LLM Settings**, configure and enable your model preset, check connectivity, and select the text and embedding models your provider serves.
2. In **Mail Settings**, add an account, choose its mailboxes, and start sync. Gmail has a dedicated [Google OAuth setup](doc/google-oauth.md).
3. Set your role, priorities, and interest profiles in **Triage Settings**. As processing finishes, explore the dashboard, topics, and search.

The first build downloads dependencies. The database persists under `docker-data/postgres/` and encryption keys in the `mailintel_secrets` Docker volume; restarting containers preserves them. The app listens on localhost; PostgreSQL is accessible within the Compose network.

## Learn more

- [Installation](doc/installation.md): model connectivity, local Python, and development setup.
- [User guide](doc/user-guide.md): accounts, triage, topics, stars, search, and processing controls.
- [Operations](doc/operations.md): settings, backups, authentication, and troubleshooting.
- [Documentation index](doc/README.md): API and architecture references.
- [Contributing](CONTRIBUTING.md): tests, database development, and source packages.

MailIntel runs one application instance with its background workers. It is designed for a private installation with a shared bearer token granting access to the whole application.
