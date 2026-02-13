# ===============================================================================
# MinIO - S3-Compatible Object Storage Container Image
# ===============================================================================
# Built from source due to CVE fix (Privilege Escalation via Session Policy
# Bypass in Service Accounts and STS) - no pre-built Docker image available
# for RELEASE.2025-10-15T17-29-55Z.
#
# Source: https://github.com/karlspace/MinIO (fork)
# Documentation: https://min.io/docs/minio/container/index.html
# ===============================================================================

# ---------------------------------------------------------------------------
# Build Arguments
# ---------------------------------------------------------------------------
ARG BASE_GO_IMAGE=golang
ARG BASE_GO_VERSION=1.26-alpine
ARG BASE_MINIO_REPO=https://github.com/karlspace/MinIO.git
ARG BASE_MINIO_VERSION=RELEASE.2025-10-15T17-29-55Z

# ---------------------------------------------------------------------------
# Stage 1: Build MinIO from source
# ---------------------------------------------------------------------------
FROM ${BASE_GO_IMAGE}:${BASE_GO_VERSION} AS builder

ARG BASE_MINIO_REPO
ARG BASE_MINIO_VERSION

RUN apk add --no-cache git

WORKDIR /src
RUN git clone ${BASE_MINIO_REPO} . \
    && git checkout ${BASE_MINIO_VERSION}

RUN CGO_ENABLED=0 go build -trimpath \
    -tags kqueue \
    -ldflags "-s -w" \
    -o /go/bin/minio .

# ---------------------------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------------------------
FROM alpine:3.23

# ---------------------------------------------------------------------------
# Metadata Labels (Legacy)
# ---------------------------------------------------------------------------
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <karl.bauer@bauer-group.com>"

# ---------------------------------------------------------------------------
# OCI Image Spec Labels
# https://github.com/opencontainers/image-spec/blob/main/annotations.md
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="MinIO"
LABEL org.opencontainers.image.description="MinIO S3-Compatible Object Storage - BAUER GROUP Edition (built from source)"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/CS-MinIO#readme"
LABEL org.opencontainers.image.version="0.1.0"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
RUN apk add --no-cache curl ca-certificates

# ---------------------------------------------------------------------------
# Binary
# ---------------------------------------------------------------------------
COPY --from=builder /go/bin/minio /usr/bin/minio

# ---------------------------------------------------------------------------
# Non-root User & Data Directory
# ---------------------------------------------------------------------------
RUN addgroup -g 1000 minio \
    && adduser -u 1000 -G minio -h /data -D minio \
    && mkdir -p /data \
    && chown minio:minio /data

EXPOSE 9000 9001
VOLUME ["/data"]

USER minio

# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
# MinIO exposes /minio/health/live on the S3 API port (9000)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=4 \
    CMD curl -f http://localhost:9000/minio/health/live || exit 1

ENTRYPOINT ["minio"]
CMD ["server", "/data"]
