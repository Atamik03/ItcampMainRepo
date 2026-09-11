# syntax=docker/dockerfile:1
#
# Frontend reverse-proxy (compose service name: "frontend-nginx", container role "docker-front-nginx")
# Serves the static Vite build (mounted from the `frontend-dist` shared volume at
# /usr/share/nginx/html) and reverse-proxies API/WebSocket paths to the backend.
#
# Base is nginxinc/nginx-unprivileged, which already runs as a non-root user and
# listens on 8080 by default (no NET_BIND_SERVICE capability required) -- so no
# extra USER directive is strictly needed. We add one anyway for explicitness/
# defense-in-depth; see DOCKER_README.md for why uid 101 is used (it's the `nginx`
# user baked into this base image).

# Pinned via scripts/pin-digests.*; re-run that script to move to a newer digest.
FROM nginxinc/nginx-unprivileged:1.27-alpine@sha256:65e3e85dbaed8ba248841d9d58a899b6197106c23cb0ff1a132b7bfe0547e4c0

# The base image's OWN Alpine packages (libcrypto3/libssl3/libxml2/libpng/
# musl/nghttp2-libs/zlib/c-ares) routinely lag behind Alpine's own package
# repo -- nginxinc rebuilds this image on their own schedule, not the
# instant a CVE fix lands in Alpine. `apk upgrade` pulls whatever patched
# package versions Alpine's repo already has for the pinned base digest
# above, closing that gap without waiting on nginxinc's next rebuild.
# Confirmed via Trivy image scan (see .github/workflows/ci.yml) that this
# alone resolves every HIGH/CRITICAL finding that had `status: fixed` in
# Alpine's repo at the time this Dockerfile was written; re-run the
# pipeline's Trivy image-scan job to verify it still does on any later
# build, since Alpine's repo state moves over time.
# nginxinc/nginx-unprivileged already defaults to a non-root user (101) in
# its OWN Dockerfile, before this one even starts -- apk needs root to
# write its package database, so switch to root for this step only, then
# back to 101 below (the same user the base image would have defaulted to
# anyway; this Dockerfile just makes it explicit).
USER root
RUN apk update && apk upgrade --no-cache && rm -rf /var/cache/apk/*

COPY docker/frontend-nginx/nginx.conf /etc/nginx/conf.d/default.conf

USER 101

EXPOSE 8080
