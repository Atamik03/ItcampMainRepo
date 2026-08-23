#!/usr/bin/env bash
# start.sh
#
# One-command local bring-up for the whole Docker Compose stack:
#   1. check prerequisites (docker, docker compose v2, openssl)
#   2. create needed directories (./data/postgres)
#   3. prepare ./.env (non-secret config) if missing
#   4. prepare ./secrets/*.txt|*.conf (Docker Secrets) if missing --
#      NEVER regenerates an existing secret: Postgres is initialized with
#      whatever password existed the first time its data directory was
#      created, so silently rotating secrets/postgres_password.txt on a
#      later run would lock you out of your own existing data volume.
#   5. pin base images to an immutable digest if not already pinned
#   6. docker compose build
#   7. docker compose up -d
#   8. wait for db/redis/backend/frontend-nginx to report healthy
#   9. print the URL to open
#
# Safe to re-run any time: every step is idempotent (skips work that's
# already done) and re-running never destroys existing data or secrets.
#
# Usage:
#   ./start.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---- small output helpers -------------------------------------------------
c_green='\033[0;32m'; c_yellow='\033[0;33m'; c_red='\033[0;31m'; c_reset='\033[0m'
info()  { printf "%b==>%b %s\n" "$c_green" "$c_reset" "$1"; }
warn()  { printf "%b!!%b %s\n" "$c_yellow" "$c_reset" "$1"; }
fail()  { printf "%b✗%b %s\n" "$c_red" "$c_reset" "$1" >&2; exit 1; }

# ---- 1. prerequisites ------------------------------------------------------
info "Checking prerequisites..."
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not on PATH. Install Docker Desktop first."
docker info >/dev/null 2>&1 || fail "Docker daemon is not reachable. Is Docker Desktop running?"
docker compose version >/dev/null 2>&1 || fail "docker compose (v2, the 'docker compose' plugin) is required. Update Docker Desktop."
command -v openssl >/dev/null 2>&1 || fail "openssl is required to generate secrets (ships with Git Bash on Windows, and by default on Linux/macOS)."

# ---- 2. directories ---------------------------------------------------------
info "Ensuring ./data/postgres exists..."
mkdir -p ./data/postgres

# ---- 3. .env (non-secret config) -------------------------------------------
if [[ -f .env ]]; then
  info ".env already exists, leaving it untouched."
else
  info "Creating .env from .env.example..."
  cp .env.example .env
fi

# ---- 4. secrets -------------------------------------------------------------
mkdir -p ./secrets
gen_secret() { openssl rand -base64 48 | tr -d '\n=+/' | head -c 40; }

new_postgres_password=false
if [[ -f ./secrets/postgres_password.txt ]]; then
  info "secrets/postgres_password.txt already exists, keeping it (Postgres was initialized with this password)."
else
  info "Generating secrets/postgres_password.txt..."
  gen_secret > ./secrets/postgres_password.txt
  chmod 600 ./secrets/postgres_password.txt 2>/dev/null || true
  new_postgres_password=true
fi

if [[ -f ./secrets/redis_password.txt ]]; then
  info "secrets/redis_password.txt already exists, keeping it."
else
  info "Generating secrets/redis_password.txt..."
  gen_secret > ./secrets/redis_password.txt
  chmod 600 ./secrets/redis_password.txt 2>/dev/null || true
fi

if [[ -f ./secrets/elou_auth_secret.txt ]]; then
  info "secrets/elou_auth_secret.txt already exists, keeping it."
else
  info "Generating secrets/elou_auth_secret.txt..."
  gen_secret > ./secrets/elou_auth_secret.txt
  chmod 600 ./secrets/elou_auth_secret.txt 2>/dev/null || true
fi

# Composed files (redis.conf `include` line, and the *_URL_FILE convention
# read by elou_avt_twin/persistence/db.py / realtime/redis_bus.py) are
# regenerated every run from the raw secrets above, since they don't carry
# any independent state of their own -- only the raw passwords above do.
info "Refreshing derived secret files (redis_requirepass.conf, database_url.txt, redis_url.txt)..."
POSTGRES_PASSWORD="$(cat ./secrets/postgres_password.txt)"
REDIS_PASSWORD="$(cat ./secrets/redis_password.txt)"
# shellcheck disable=SC1091
source .env
POSTGRES_USER="${POSTGRES_USER:-elou_avt}"
POSTGRES_DB="${POSTGRES_DB:-elou_avt}"

printf 'requirepass %s\n' "$REDIS_PASSWORD" > ./secrets/redis_requirepass.conf
chmod 600 ./secrets/redis_requirepass.conf 2>/dev/null || true

printf 'postgresql://%s:%s@db:5432/%s' "$POSTGRES_USER" "$POSTGRES_PASSWORD" "$POSTGRES_DB" > ./secrets/database_url.txt
chmod 600 ./secrets/database_url.txt 2>/dev/null || true

printf 'redis://:%s@redis:6379/0' "$REDIS_PASSWORD" > ./secrets/redis_url.txt
chmod 600 ./secrets/redis_url.txt 2>/dev/null || true

if $new_postgres_password; then
  warn "A brand-new Postgres password was generated. If ./data/postgres already contains an initialized cluster from a PREVIOUS password, Postgres will fail to authenticate. Only remove ./data/postgres yourself if you intend to discard existing data."
fi

# ---- 5. digest pinning ------------------------------------------------------
# A FROM line is "unpinned" if it has no "@sha256:" after the image:tag.
unpinned=0
for f in docker/backend/Dockerfile docker/frontend-build/Dockerfile docker/frontend-nginx/Dockerfile; do
  if grep -E '^FROM ' "$f" | grep -qv '@sha256:'; then
    unpinned=1
  fi
done
grep -E '^\s*image:' docker-compose.yml | grep -qv '@sha256:' && unpinned=1

if [[ "$unpinned" -eq 0 ]]; then
  info "Base images already pinned to a digest, skipping scripts/pin-digests.sh."
else
  info "Pinning base images to an immutable digest (scripts/pin-digests.sh)..."
  bash scripts/pin-digests.sh
fi

# ---- 6. build ----------------------------------------------------------------
info "Building images (docker compose build)..."
docker compose build

# ---- 7. up ---------------------------------------------------------------------
info "Starting the stack (docker compose up -d)..."
docker compose up -d

# ---- 8. wait for health -------------------------------------------------------
info "Waiting for services to report healthy..."
services_to_check=(db redis backend frontend-nginx)
deadline=$((SECONDS + 180))
for svc in "${services_to_check[@]}"; do
  cid="$(docker compose ps -q "$svc")"
  [[ -z "$cid" ]] && fail "Service '$svc' has no container -- did 'docker compose up -d' fail?"
  while true; do
    status="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$cid")"
    if [[ "$status" == "healthy" || "$status" == "no-healthcheck" ]]; then
      info "  $svc: $status"
      break
    fi
    if [[ "$status" == "unhealthy" ]]; then
      fail "$svc reported unhealthy. Check: docker compose logs $svc"
    fi
    if (( SECONDS > deadline )); then
      fail "Timed out waiting for $svc to become healthy. Check: docker compose logs $svc"
    fi
    sleep 2
  done
done

echo
info "Stack is up."
echo "  Open the app:      http://localhost:8080"
echo "  Backend API docs:  http://localhost:8080/docs"
echo "  Status:             ./status.sh"
echo "  Logs (follow):      docker compose logs -f backend"
echo "  Stop:                ./stop.sh"
