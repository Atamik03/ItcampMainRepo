# syntax=docker/dockerfile:1
#
# Frontend build job (compose service name: "frontend-build", container role "docker-js")
# One-shot: compiles the Vite/React/TS app in elou_avt_web/ into static assets.
# Output lands in /app/dist, which docker-compose.yml mounts as the named volume
# `frontend-dist`, shared read-only with the frontend-nginx container.
#
# Build context is the REPO ROOT (see docker-compose.yml `frontend-build.build.context: .`).

# Pinned via scripts/pin-digests.*; re-run that script to move to a newer digest.
#
# node:22-alpine, not node:20: node:20-alpine's bundled npm CLI vendors
# old js-yaml/pacote/tar with HIGH/CRITICAL CVEs (CVE-2026-84375,
# CVE-2026-9496, CVE-2026-73566 -- caught by this pipeline's Trivy image
# scan). Pinning `npm install -g npm@<version>` to something old enough
# to still support Node 20 doesn't actually clear them: the newest npm
# release compatible with Node 20 (11.6.4) still resolves pacote/tar
# ranges that npm audit reports as vulnerable to the same CVEs, and
# npm@latest (12.x) requires Node >=22 and fails outright (EBADENGINE)
# against Node 20 -- confirmed both ways before switching the base
# image. Node 22 is still an active LTS line (maintenance until
# 2027-04-30) and its own bundled npm plus an `npm install -g npm@...`
# upgrade both resolve to a CVE-fixed pacote version (tar/js-yaml still
# need the explicit pin below).
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build

WORKDIR /app

# chown: WORKDIR creates /app as root (USER node hasn't taken effect
# yet), and `COPY --chown` below only chowns the files it copies, not
# the /app directory entry itself -- without this, the `node` user can
# read the copied files but can't create NEW files in /app (e.g.
# tsconfig.tsbuildinfo written during `tsc -b`), and `npm run build`
# fails with EACCES.
#
# npm install -g npm@12.0.2: belt-and-suspenders on top of the Node 22
# base image bump above -- makes sure the bundled npm CLI itself stays
# current rather than whatever shipped with this particular Alpine
# rebuild. Pinned to an exact version, not `@latest` (hadolint DL3016:
# pin versions in npm install).
#
# The `npm install --prefix "$(npm root -g)/npm" tar@7.5.21
# js-yaml@4.3.2 --no-save` that follows fixes something npm@12.0.2
# alone does not: npm vendors tar/js-yaml as PRIVATE dependencies
# inside its own node_modules (not the global node_modules any `npm
# install -g <pkg>` would touch), and npm@12.0.2 itself still pins tar
# to ^7.5.19 and js-yaml transitively -- both resolve to the OLDEST
# version satisfying that range, which is still inside the vulnerable
# range for CVE-2026-73566 (fix: 7.5.21) and CVE-2026-84375 (fix:
# 4.3.2), confirmed via this pipeline's own Trivy image scan even after
# the node:22-alpine + npm upgrade above. Upgrading the copies inside
# npm's own node_modules directly, pinned to the exact fixed versions
# (hadolint DL3016), is the only way to reach them without npm itself
# cutting a new release. `--prefix` instead of `cd` (hadolint DL3003:
# use WORKDIR, not cd, to change directory -- `cd` in a RUN here would
# only need to apply to one command in the chain, so WORKDIR itself is
# the wrong tool; --prefix avoids `cd` entirely).
# Combined into one RUN with the chown above (hadolint DL3059: no
# consecutive RUNs).
RUN chown node:node /app \
    && npm install -g npm@12.0.2 \
    && npm install --prefix "$(npm root -g)/npm" tar@7.5.21 js-yaml@4.3.2 --no-save

# Copy manifest files first for better Docker layer caching: `npm ci` only
# re-runs when package.json/package-lock.json actually change.
COPY --chown=node:node elou_avt_web/package.json elou_avt_web/package-lock.json ./
RUN npm ci

COPY --chown=node:node elou_avt_web/ ./

# node:22-alpine ships a non-root `node` user (uid 1000) out of the box.
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

