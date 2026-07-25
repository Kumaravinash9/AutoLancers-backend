# automateLancers — backend

Freelance auto-bid assistant. Polls Freelancer.com for new projects, scores them against your
profile, and drafts a tailored proposal with Claude for each good match.

**v1 is draft-only.** Nothing is ever submitted to Freelancer.com — you review each draft and copy
it across yourself. The `bid` OAuth scope is deliberately not requested.

The frontend lives in a separate repo: `automateLancers-frontend`.

## Requirements

- Python 3.12+
- PostgreSQL 14+
- A Freelancer.com OAuth app (self-service at https://accounts.freelancer.com/settings/create_app)
- An Anthropic API key

## Setup

```bash
uv sync
docker compose up -d      # Postgres on port 5434
cp .env.example .env      # then fill it in
uv run alembic upgrade head
```

Postgres runs on **5434** and the API on **8010** rather than the usual 5432/8000, because both of
those are commonly already taken on a dev machine and a silent port collision is a confusing
failure. Change them in `.env` if you prefer — the OAuth redirect URI must match whatever you pick,
both here and on the registered Freelancer app.

Generate the token encryption key for `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Connect your Freelancer account

```bash
uv run python scripts/oauth_login.py
```

Opens a browser, completes the OAuth authorization-code flow, and stores the resulting token
encrypted in Postgres. Refresh is automatic from then on.

## Run

```bash
uv run uvicorn app.main:app --reload --port 8010
```

The poller starts with the app and runs every `POLL_INTERVAL_SECONDS` (default 25s).
`POST /pipeline/run` triggers a single cycle by hand.

## Test

```bash
uv run pytest
```

## Notes on the Freelancer.com API

- Discovery: `GET /api/projects/0.1/projects/active/`. Polling is the only option — there is no
  public webhook or SSE. The live "Project Alerts" panel on the website runs on an internal,
  session-authenticated socket that is not part of the public API.
- Auth uses Freelancer's own header, not `Authorization: Bearer`.
- Their terms require cached data to be refreshed at least every 24h; `prune_stale_jobs` handles it.
