#!/usr/bin/env bash
# status.sh -- quick health snapshot of the stack.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

echo "==> Container status"
docker compose ps

echo
echo "==> Health"
for svc in db redis backend frontend-nginx; do
  cid="$(docker compose ps -q "$svc" 2>/dev/null || true)"
  if [[ -z "$cid" ]]; then
    printf "  %-16s not running\n" "$svc"
    continue
  fi
  status="$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}running (no healthcheck){{end}}' "$cid")"
  printf "  %-16s %s\n" "$svc" "$status"
done

echo
if curl -s -o /dev/null -w '' "http://localhost:8080/health" 2>/dev/null; then
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/health)"
  echo "==> App reachable at http://localhost:8080 (backend /health -> HTTP $code)"
else
  echo "==> App not reachable at http://localhost:8080"
fi
