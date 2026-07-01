# ===============================================================================
# MinIO Worker - Generic Webhook-Driven Helper
# ===============================================================================
# Optional, long-running helper container for the MinIO stack (Compose profile:
# "worker"). Receives MinIO bucket-notification webhooks, enqueues durable jobs,
# and a background worker drains them.
#
# Current job: CDN cache purge (Cloudflare + Bunny) on object create/delete.
# Plugin framework: new event sources are handlers/ modules, new purge targets are
# providers/ modules - both self-register.
#
# Documentation: https://github.com/bauer-group/CS-MinIO
# ===============================================================================

FROM python:3.14-alpine

# ---------------------------------------------------------------------------
# Metadata Labels (Legacy)
# ---------------------------------------------------------------------------
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <karl.bauer@bauer-group.com>"

# ---------------------------------------------------------------------------
# OCI Image Spec Labels
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="MinIO Worker"
LABEL org.opencontainers.image.description="Generic webhook-driven worker for the MinIO stack (CDN cache purge: Cloudflare + Bunny)"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/CS-MinIO#readme"
LABEL org.opencontainers.image.version="0.8.2"

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
ENV PYTHONUNBUFFERED=1
# Ensure main.py can import the app modules (tasks, config, providers, ...).
ENV PYTHONPATH=/app

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
RUN apk add --no-cache \
        curl \
        tini \
    && rm -rf /var/cache/apk/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
COPY . .

# ---------------------------------------------------------------------------
# Non-root User
# ---------------------------------------------------------------------------
RUN addgroup -g 1000 worker \
    && adduser -u 1000 -G worker -h /app -D worker \
    && mkdir -p /data/queue \
    && chown -R worker:worker /app /data

USER worker

EXPOSE 8080

# ---------------------------------------------------------------------------
# Health & Entrypoint (tini for proper signal handling)
# ---------------------------------------------------------------------------
# One built-in check for both roles (uses WORKER_MODE; auto-detects when unset).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "healthcheck.py"]

# main.py dispatches on WORKER_MODE (receiver | consumer) — same image for both roles.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["python", "main.py"]
