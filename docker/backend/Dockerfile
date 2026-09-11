# syntax=docker/dockerfile:1
#
# Backend service (compose service name: "backend", container role "docker-beck")
# FastAPI app served by uvicorn, entrypoint at elou_avt_twin/api_server.py (ASGI app object: `app`).
#
# Build context is the REPO ROOT (see docker-compose.yml `backend.build.context: .`),
# so paths below are relative to the repository root, not this docker/backend/ folder.

########################################
# Stage 1: builder
########################################
# Pinned via scripts/pin-digests.*; re-run that script to move to a newer digest.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

# numpy/scikit-learn/scipy are real compiled C/Fortran extensions. If no prebuilt
# wheel exists for this exact Python version + CPU architecture, pip falls back to
# building from source, which requires a C/C++ toolchain. Installed only in this
# builder stage so the compiler never reaches the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv keeps the installed site-packages in one directory we can copy
# wholesale into the final stage, instead of copying system site-packages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY elou_avt_twin/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip==26.2.1 \
    && pip install --no-cache-dir -r requirements.txt

########################################
# Stage 2: final runtime image
########################################
# Pinned via scripts/pin-digests.*; re-run that script to move to a newer digest.
FROM python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS final

# The base image's OWN Debian packages (util-linux family: bsdutils/
# libblkid1/liblastlog2-2/libmount1/libsmartcols1/libuuid1/login/mount)
# routinely lag behind Debian's own security repo -- the upstream python
# image is rebuilt on its own schedule, not the instant a CVE fix lands in
# Debian. This pulls whatever patched package versions Debian's security
# repo already has for the pinned base digest above, closing that gap
# without waiting on the next upstream rebuild. Confirmed via Trivy image
# scan (see .github/workflows/ci.yml) that this resolves every HIGH/CRITICAL
# finding that had `status: fixed` at the time this Dockerfile was written;
# the remaining findings (gzip, libacl1, ncurses, openssl, perl-base) have
# no fix published anywhere yet and are tracked in .trivyignore instead.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# Non-root user. Fixed uid/gid so it can be pinned in docker-compose.yml's
# `user:` field too (10001:10001), independent of build-time /etc/passwd allocation.
RUN groupadd -r appuser --gid 10001 \
    && useradd -r -g appuser -u 10001 --create-home appuser

# Only the venv (no compiler/toolchain) crosses over from the builder stage.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=appuser:appuser elou_avt_twin/ /app/

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

# CLI form (not `python api_server.py`) so --host/--port are set explicitly for
# container networking; api_server.py's own __main__ block binds 127.0.0.1 only,
# which would be unreachable from other containers.
CMD ["python", "-m", "uvicorn", "api_server:app", "--host", "0.0.0.0", "--port", "8000"]
