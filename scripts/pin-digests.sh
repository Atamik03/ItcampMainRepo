#!/usr/bin/env bash
# scripts/pin-digests.sh
#
# WHY THIS IS A SCRIPT AND NOT HARDCODED DIGESTS:
# Base image digests change every time the upstream maintainers rebuild that
# image (e.g. to ship an OS security patch), even when the tag stays the same.
# Hardcoding today's digest directly into the Dockerfiles/compose file would go
# stale: eventually you'd either be pinned to an old, unpatched image forever,
# or (if the tag is later garbage-collected upstream) pulling a hash that no
# longer resolves at all. This script is meant to be re-run DELIBERATELY,
# on each intentional version bump, by someone with real registry access
# (which this sandbox does not have) -- not baked in blindly.
#
# Usage: run from the repo root.
#   ./scripts/pin-digests.sh
#
# For each base image used in this stack, pulls it, resolves the digest, and
# rewrites the matching placeholder-marked FROM/image line in place.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# image:tag -> file(s) containing the literal "image:tag" string to replace
declare -A TARGETS=(
  ["python:3.12-slim"]="docker/backend/Dockerfile"
  ["node:20-alpine"]="docker/frontend-build/Dockerfile"
  ["nginxinc/nginx-unprivileged:1.27-alpine"]="docker/frontend-nginx/Dockerfile"
  ["postgres:16-alpine"]="docker-compose.yml"
  ["redis:7-alpine"]="docker-compose.yml"
)

for image in "${!TARGETS[@]}"; do
  echo "==> Pulling ${image}"
  docker pull "${image}"

  digest="$(docker inspect --format='{{index .RepoDigests 0}}' "${image}")"
  if [[ -z "${digest}" ]]; then
    echo "!! Could not resolve a RepoDigest for ${image}, skipping" >&2
    continue
  fi

  # ${digest} is already "repo/name@sha256:...."; we want to replace the plain
  # "image:tag" occurrence with "image:tag@sha256:...".
  sha_only="${digest##*@}"
  pinned="${image}@${sha_only}"
  echo "    ${image}  ->  ${pinned}"

  for file in ${TARGETS[$image]}; do
    if [[ -f "${file}" ]]; then
      # Match "image:tag" with an OPTIONAL pre-existing "@sha256:..." suffix
      # and replace the whole thing with the freshly resolved pin. Without
      # the optional suffix, re-running this script against an
      # already-pinned line would append a second "@sha256:..." instead of
      # replacing the first one.
      escaped_image="$(printf '%s' "${image}" | sed -e 's/[.[\*^$/]/\\&/g')"
      sed -i -E "s#${escaped_image}(@sha256:[0-9a-f]+)*#${pinned}#g" "${file}"
      echo "    updated ${file}"
    else
      echo "    !! ${file} not found, skipping" >&2
    fi
  done
done

echo
echo "Done. Review the diffs (git diff) before committing pinned digests."
