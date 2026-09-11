# syntax=docker/dockerfile:1
#
# Frontend build job (compose service name: "frontend-build", container role "docker-js")
# One-shot: compiles the Vite/React/TS app in elou_avt_web/ into static assets.
# Output lands in /app/dist, which docker-compose.yml mounts as the named volume
# `frontend-dist`, shared read-only with the frontend-nginx container.
#
# Build context is the REPO ROOT (see docker-compose.yml `frontend-build.build.context: .`).

# Pinned via scripts/pin-digests.*; re-run that script to move to a newer digest.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

WORKDIR /app

# WORKDIR creates /app as root (USER node hasn't taken effect yet), and
# `COPY --chown` below only chowns the files it copies, not the /app
# directory entry itself -- without this, the `node` user can read the
# copied files but can't create NEW files in /app (e.g. tsconfig.tsbuildinfo
# written during `tsc -b`), and `npm run build` fails with EACCES.
RUN chown node:node /app

# node:20-alpine ships its own bundled npm CLI with its own vendored
# dependencies (js-yaml, pacote, tar, ...) -- those drift out of date
# between Alpine base-image rebuilds the same way OS packages do (see
# docker/backend/Dockerfile's `apt-get upgrade` and
# docker/frontend-nginx/Dockerfile's `apk upgrade` for the same pattern).
# Confirmed via Trivy image scan that the pinned digest's bundled npm
# carries HIGH findings (CVE-2026-84375 js-yaml, CVE-2026-9496 pacote,
# CVE-2026-73566 tar) with no way to reach them except replacing npm
# itself -- they are not project dependencies.
RUN npm install -g npm@latest

# Copy manifest files first for better Docker layer caching: `npm ci` only
# re-runs when package.json/package-lock.json actually change.
COPY --chown=node:node elou_avt_web/package.json elou_avt_web/package-lock.json ./
RUN npm ci

COPY --chown=node:node elou_avt_web/ ./

# node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 ships a non-root `node` user (uid 1000) out of the box.
USER node

RUN npm run build

# Bake the freshly-built assets into a path OTHER than /app/dist. If we kept
# them at /app/dist, docker-compose.yml's `frontend-dist:/app/dist` volume
# mount would shadow that path the moment the CONTAINER starts -- Docker only
# auto-populates a named volume from the image's content the FIRST time that
# volume is ever used (i.e. when it's still empty). On every later rebuild,
# the (by then stale) existing volume content would silently keep being
# served forever, no matter how many times the image is rebuilt -- exactly
# the trap that bit a real fix in this repo once already. Copying explicitly
# on every container start (see CMD below) makes rebuilds actually take
# effect instead of only working "the first time".
RUN mkdir -p /app/dist-baked && cp -r /app/dist/. /app/dist-baked/

# Copies the image's freshly-built assets into the (possibly stale,
# possibly volume-shadowed) /app/dist on every start, so re-running this
# one-shot job after a rebuild always refreshes what frontend-nginx serves,
# not just the very first time the frontend-dist volume was created.
CMD ["sh", "-c", "rm -rf /app/dist/* && cp -r /app/dist-baked/. /app/dist/ && echo 'frontend-build: dist/ refreshed in the shared volume, exiting'"]

