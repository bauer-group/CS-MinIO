# ===============================================================================
# MinIO Admin Console - Web-based Management UI
# ===============================================================================
# Built from source (karlspace/MinIO-UI fork).
# 3-stage build: React frontend + Go backend + Alpine runtime.
#
# Source: https://github.com/karlspace/MinIO-UI
# ===============================================================================

# ---------------------------------------------------------------------------
# Build Arguments
# ---------------------------------------------------------------------------
ARG BASE_NODE_IMAGE=node
ARG BASE_NODE_VERSION=22-alpine
ARG BASE_GO_IMAGE=golang
ARG BASE_GO_VERSION=1.26-alpine
ARG BASE_CONSOLE_REPO=https://github.com/karlspace/MinIO-UI.git
ARG BASE_CONSOLE_VERSION=feature/admin-console-v2

# ---------------------------------------------------------------------------
# Stage 1: Build Frontend (React/TypeScript)
# ---------------------------------------------------------------------------
FROM ${BASE_NODE_IMAGE}:${BASE_NODE_VERSION} AS frontend-builder

ARG BASE_CONSOLE_REPO
ARG BASE_CONSOLE_VERSION

RUN apk add --no-cache git

WORKDIR /workspace
RUN git clone --depth 1 --branch ${BASE_CONSOLE_VERSION} ${BASE_CONSOLE_REPO} .

WORKDIR /workspace/web-app
RUN corepack enable && corepack prepare && yarn install && yarn build

# ---------------------------------------------------------------------------
# Stage 2: Build Backend (Go)
# ---------------------------------------------------------------------------
FROM ${BASE_GO_IMAGE}:${BASE_GO_VERSION} AS backend-builder

RUN apk add --no-cache ca-certificates tzdata git

WORKDIR /workspace

# Copy source from frontend stage (includes full repo)
COPY --from=frontend-builder /workspace/go.mod /workspace/go.sum ./
RUN go mod download

COPY --from=frontend-builder /workspace/ ./
COPY --from=frontend-builder /workspace/web-app/build ./web-app/build

RUN CGO_ENABLED=0 go build -trimpath \
    -tags kqueue \
    -ldflags "-s -w" \
    -o /go/bin/console \
    ./cmd/console

# ---------------------------------------------------------------------------
# Stage 3: Runtime
# ---------------------------------------------------------------------------
FROM alpine:3.23

# ---------------------------------------------------------------------------
# Metadata Labels (Legacy)
# ---------------------------------------------------------------------------
LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <karl.bauer@bauer-group.com>"

# ---------------------------------------------------------------------------
# OCI Image Spec Labels
# ---------------------------------------------------------------------------
LABEL org.opencontainers.image.title="MinIO Admin Console"
LABEL org.opencontainers.image.description="Web-based admin console for MinIO object storage"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <karl.bauer@bauer-group.com>"
LABEL org.opencontainers.image.licenses="AGPL-3.0"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.url="https://github.com/bauer-group/CS-MinIO"
LABEL org.opencontainers.image.documentation="https://github.com/bauer-group/CS-MinIO#readme"
LABEL org.opencontainers.image.version="0.2.2"

# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
RUN apk add --no-cache ca-certificates curl tzdata

# ---------------------------------------------------------------------------
# Non-root User
# ---------------------------------------------------------------------------
RUN addgroup -g 1000 console \
    && adduser -u 1000 -G console -h /app -D console

WORKDIR /app

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
COPY --from=backend-builder /go/bin/console /app/console
COPY --from=frontend-builder /workspace/entrypoint.sh /app/entrypoint.sh

RUN sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh \
    && chown -R console:console /app

USER console

EXPOSE 9090

# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=4 \
    CMD curl -sf http://localhost:9090/ || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["server"]
