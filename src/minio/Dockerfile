# ===============================================================================
# MinIO - S3-Compatible Object Storage Container Image
# ===============================================================================
# Base Image: https://quay.io/repository/minio/minio
# Documentation: https://min.io/docs/minio/container/index.html
# ===============================================================================

# ---------------------------------------------------------------------------
# Build Arguments (Base Image)
# ---------------------------------------------------------------------------
# These ARGs define the BASE image to build FROM.
# Default: Official MinIO image from Quay.io.
# Override via: docker build --build-arg BASE_MINIO_IMAGE=... --build-arg BASE_MINIO_VERSION=...
ARG BASE_MINIO_IMAGE=quay.io/minio/minio
ARG BASE_MINIO_VERSION=RELEASE.2025-10-15T17-29-55Z

FROM ${BASE_MINIO_IMAGE}:${BASE_MINIO_VERSION}

# Re-declare ARGs after FROM (without defaults to inherit passed values)
ARG BASE_MINIO_IMAGE
ARG BASE_MINIO_VERSION

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
LABEL org.opencontainers.image.description="MinIO S3-Compatible Object Storage - BAUER GROUP Edition"
LABEL org.opencontainers.image.version="0.1.0"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/CS-MinIO#readme"
LABEL org.opencontainers.image.base.name="${BASE_MINIO_IMAGE}:${BASE_MINIO_VERSION}"

# ---------------------------------------------------------------------------
# Customizations
# ---------------------------------------------------------------------------
# Currently no customizations - using base image as-is.
# Add custom configurations, scripts, or certificates here if needed.

# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
# MinIO exposes /minio/health/live on the S3 API port (9000)
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=4 \
    CMD curl -f http://localhost:9000/minio/health/live || exit 1
