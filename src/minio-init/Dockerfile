# ===============================================================================
# MinIO Init - Declarative Object Storage Initialization
# ===============================================================================
# One-shot container that configures MinIO from a JSON configuration file.
#
# Features:
#   - Bucket creation (versioning, quotas, retention, anonymous policy)
#   - Custom IAM policy management
#   - Group creation with policy attachment
#   - User creation with group membership
#   - Service account provisioning (dynamic credentials)
#   - Idempotent - safe to run on every start
#
# Documentation: https://github.com/bauer-group/CS-MinIO
# ===============================================================================

# ---------------------------------------------------------------------------
# Build Arguments
# ---------------------------------------------------------------------------
ARG BASE_GO_IMAGE=golang
ARG BASE_GO_VERSION=1.26-alpine
ARG BASE_MC_REPO=https://github.com/karlspace/MinIO-CLI.git
ARG BASE_MC_VERSION=master

# ---------------------------------------------------------------------------
# Stage 1: Build mc client from source
# ---------------------------------------------------------------------------
FROM ${BASE_GO_IMAGE}:${BASE_GO_VERSION} AS mc-builder

ARG BASE_MC_REPO
ARG BASE_MC_VERSION

RUN apk add --no-cache git

WORKDIR /src
RUN git clone --depth 1 --branch ${BASE_MC_VERSION} ${BASE_MC_REPO} .

RUN CGO_ENABLED=0 go build -trimpath \
    -tags kqueue \
    -ldflags "-s -w" \
    -o /go/bin/mc .

# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
FROM python:3.14-alpine

# ---------------------------------------------------------------------------
# Metadata Labels (Legacy)
# ---------------------------------------------------------------------------
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <karl.bauer@bauer-group.com>"

# ---------------------------------------------------------------------------
# OCI Image Spec Labels
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="MinIO Init"
LABEL org.opencontainers.image.description="Declarative MinIO initialization from JSON configuration"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/CS-MinIO#readme"
LABEL org.opencontainers.image.version="0.2.0"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
COPY --from=mc-builder /go/bin/mc /usr/local/bin/mc
RUN apk add --no-cache \
        curl \
        tini \
    && pip install --no-cache-dir rich \
    && rm -rf /var/cache/apk/*

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
WORKDIR /app

COPY main.py .
COPY tasks/ ./tasks/
COPY config/ ./config/

# ---------------------------------------------------------------------------
# Non-root User
# ---------------------------------------------------------------------------
RUN addgroup -g 1000 init \
    && adduser -u 1000 -G init -h /app -D init \
    && mkdir -p /data/credentials \
    && chown -R init:init /app /data

USER init

# ---------------------------------------------------------------------------
# Entrypoint (tini for proper signal handling)
# ---------------------------------------------------------------------------
ENTRYPOINT ["/sbin/tini", "--", "python", "main.py"]
