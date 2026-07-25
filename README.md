# AutoLancers — backend

Freelance auto-bid assistant. Polls Freelancer.com for new projects, scores them against your
profile, and drafts a tailored proposal with Claude for each good match.

**v1 is draft-only.** Nothing is ever submitted to Freelancer.com — you review each draft and copy
it across yourself. The `bid` OAuth scope is deliberately not requested.

The frontend lives in a separate repo: `AutoLancers-frontend`.

## Requirements

- Python 3.12+
- PostgreSQL 14+ (a `docker compose` file is included)
- A Freelancer.com OAuth app (create one at https://accounts.freelancer.com/settings/develop)
- A Gemini API key from https://aistudio.google.com/apikey (or an Anthropic key — see below)

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

## Proposal drafting

Drafts are written by an LLM, selected with `LLM_PROVIDER`:

| Provider | Setting | Default model |
|---|---|---|
| Google Gemini (default) | `LLM_PROVIDER=gemini` + `GEMINI_API_KEY` | `gemini-3.6-flash` |
| Anthropic | `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` | `claude-opus-5` |

The prompt (`app/prompts/proposal_system.md`) is provider-neutral, so switching is a config change.
It is sourced from the `freelancer-proposal` skill: the six-beat flow, the 120–180 word limit, the
conversion rules, and the honesty guardrail (never invent numbers or claims).

**Both current model families think before answering, and that thinking is drawn from the same
output budget as the reply.** On real postings a ~150-word proposal costs ~200 visible tokens but
over 1,200 thinking tokens, so `MAX_OUTPUT_TOKENS` is deliberately generous — sizing it for the
visible text alone returns an empty draft. Thinking tokens are counted into the stored
`proposal_output_tokens` so cost per bid isn't understated.

**Free-tier Gemini has per-minute and per-day request limits.** The pipeline drafts at most
`MAX_DRAFTS_PER_CYCLE` (5) per cycle, and a rate-limited draft is logged and retried next cycle
rather than lost — but on a busy day the free tier can still be the binding constraint. Lower
`POLL_INTERVAL_SECONDS` cautiously.

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
