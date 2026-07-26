#!/usr/bin/env bash
#
# Local setup, from a fresh clone to a running database.
#
#   ./scripts/bootstrap.sh
#
# Idempotent: safe to re-run. It never drops data — use `docker compose down -v` for that.
set -euo pipefail

cd "$(dirname "$0")/.."

step() { printf "\n\033[1m→ %s\033[0m\n" "$1"; }

step "Checking prerequisites"
for cmd in docker uv; do
  command -v "$cmd" >/dev/null || { echo "  missing: $cmd"; exit 1; }
done
docker info >/dev/null 2>&1 || { echo "  Docker is installed but not running."; exit 1; }
echo "  docker, uv ✓"

step "Installing Python dependencies"
uv sync --quiet
echo "  done"

step "Starting Postgres (port 5434)"
docker compose up -d >/dev/null
for _ in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -U postgres -d automatelancers >/dev/null 2>&1; then
    echo "  ready"
    break
  fi
  sleep 1
done

step "Creating .env"
if [ -f .env ]; then
  echo "  .env already exists — leaving it alone"
else
  cp .env.example .env
  # Both keys are required for the app to start; generating them here means a new developer
  # never hits a cryptic startup error on their first run.
  FERNET=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  JWT=$(uv run python -c "import secrets; print(secrets.token_urlsafe(48))")
  uv run python - "$FERNET" "$JWT" <<'PY'
import pathlib, sys
fernet, jwt = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
t = p.read_text().replace("TOKEN_ENCRYPTION_KEY=", f"TOKEN_ENCRYPTION_KEY={fernet}")
t = t.replace("JWT_SECRET=", f"JWT_SECRET={jwt}")
p.write_text(t)
PY
  echo "  written, with fresh encryption and session keys"
fi

step "Applying migrations"
uv run alembic upgrade head 2>&1 | grep -E "Running upgrade|already at" | sed 's/^/  /' || echo "  schema up to date"

step "Done"
cat <<'EOF'

  Next:
    1. Create an admin        uv run python scripts/create_admin.py you@example.com
    2. Seed some demo jobs    uv run python scripts/seed_demo_jobs.py     (optional)
    3. Run the API            uv run uvicorn app.main:app --reload --port 8010

  To draft proposals you also need GEMINI_API_KEY in .env
  (https://aistudio.google.com/apikey). Discovery and scoring work without it.

EOF
