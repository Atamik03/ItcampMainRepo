#!/usr/bin/env bash
# scripts/scan-images.sh
#
# Vulnerability-scans every image used by this stack via the official Trivy
# container image (no local Trivy install required). Run this AFTER
# `docker compose build`.
#
# Usage:
#   docker compose build
#   ./scripts/scan-images.sh

set -euo pipefail

# Compose names locally-built images "<project-dir-name>-<service>" by default,
# where <project-dir-name> is derived from the folder this repo lives in (or
# the `COMPOSE_PROJECT_NAME` env var / `name:` field if set). The guesses below
# assume the default; if they don't match what you actually built, run:
#   docker compose images
# and substitute the real names/tags below.
PROJECT_NAME="$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-')"

IMAGES=(
  "${PROJECT_NAME}-backend"
  "${PROJECT_NAME}-frontend-build"
  "${PROJECT_NAME}-frontend-nginx"
  "postgres:16-alpine"
  "redis:7-alpine"
)

echo "Images to scan (edit this list, or check 'docker compose images', if names don't match):"
printf '  - %s\n' "${IMAGES[@]}"
echo

for image in "${IMAGES[@]}"; do
  echo "==================================================================="
  echo "Scanning: ${image}"
  echo "==================================================================="
  docker run --rm \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v trivy-cache:/root/.cache/ \
    aquasec/trivy image --severity HIGH,CRITICAL "${image}" \
    || echo "!! Scan failed for ${image} (image may not exist locally -- check 'docker compose images')"
  echo
done

echo "Done. For full (non-HIGH/CRITICAL-only) output, re-run trivy without --severity."
