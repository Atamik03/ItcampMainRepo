#!/usr/bin/env bash
# stop.sh -- stop the stack. Containers are removed; data (Postgres bind
# mount + the redis-data/frontend-dist named volumes) is KEPT.
#
# Usage:
#   ./stop.sh          # stop, keep data (default, safe)
#   ./stop.sh --purge  # ALSO delete the redis-data/frontend-dist volumes
#                       # (Postgres data in ./data/postgres is a host bind
#                       # mount and is NEVER touched by this script -- if
#                       # you also want to wipe it, remove it yourself:
#                       # rm -rf ./data/postgres)

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ "${1:-}" == "--purge" ]]; then
  echo "==> Stopping and removing containers + redis-data/frontend-dist volumes..."
  docker compose down -v
  echo "    Postgres data in ./data/postgres was NOT touched (host bind mount)."
else
  echo "==> Stopping containers (data preserved)..."
  docker compose down
fi
