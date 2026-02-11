# MinIO Object Storage

Production-ready [MinIO](https://min.io/) S3-compatible object storage deployment with declarative JSON-based initialization, optional admin console, and full CI/CD automation.

## Features

- **S3-Compatible API** - Full Amazon S3 API compatibility via MinIO
- **Declarative Init Container** - JSON-based provisioning of buckets, policies, groups, users, and service accounts
- **Admin Console** - Optional third-party admin UI (replaces MinIO's removed built-in admin UI)
- **Multiple Deployment Modes** - Direct port access or Traefik reverse proxy with automatic HTTPS
- **DNS-Style Bucket Access** - Prepared virtual-host-style routing (e.g., `bucket.s3.example.com`)
- **CI/CD Automation** - Semantic releases, Docker image builds, base image monitoring, auto-merge

## Quick Start

1. **Clone the repository**
   ```bash
   git clone https://github.com/bauer-group/CS-MinIO.git
   cd CS-MinIO
   ```

2. **Create environment file**
   ```bash
   cp .env.example .env
   ```

3. **Edit `.env`** - Set at minimum:
   - `MINIO_ROOT_PASSWORD` - Admin password
   - `APP_SERVICE_SECRET_KEY` - Application service account secret
   - `CONSOLE_PASSWORD` - Console user password (if using admin console)

4. **Edit `config/minio-init.json`** - Configure buckets, policies, users as needed

5. **Start MinIO**
   ```bash
   # Single mode (direct port access)
   docker compose -f docker-compose-single.yml up -d

   # Or: Traefik mode (HTTPS via reverse proxy)
   docker compose -f docker-compose-single-traefik.yml up -d
   ```

6. **Access MinIO**

   | Mode | S3 API | Console |
   |------|--------|---------|
   | Single | `http://localhost:9000` | `http://localhost:9001` |
   | Traefik | `https://{S3_HOSTNAME}` | `https://{S3_CONSOLE_HOSTNAME}` |

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                   │
│                                                          │
│  ┌──────────────┐    ┌──────────────┐                    │
│  │ minio-server │◄───│  minio-init  │                    │
│  │              │    │  (one-shot)  │                    │
│  │  S3 API      │    │              │                    │
│  │  :9000       │    │  Applies     │                    │
│  │              │    │  JSON config │                    │
│  │  Console     │    │  on start    │                    │
│  │  :9001       │    └──────────────┘                    │
│  └──────────────┘                                        │
│         │                                                │
│         │  With admin console override:                   │
│         │  ┌────────────┐                                │
│         │  │   admin-   │  Replaces built-in console     │
│         └──│  console   │  on same port / hostname       │
│            │  :9090     │                                │
│            └────────────┘                                │
│                     Internal Network                     │
└──────────────────────────────────────────────────────────┘
```

## Deployment Modes

| Mode | Compose File | Description |
|------|-------------|-------------|
| **Single** | `docker-compose-single.yml` | Direct port binding, no reverse proxy needed |
| **Single + Traefik** | `docker-compose-single-traefik.yml` | HTTPS via Traefik with Let's Encrypt certificates |

### With Admin Console

Append the admin console override file to any deployment mode:

```bash
# Single mode with admin console
docker compose -f docker-compose-single.yml -f docker-compose-admin-console.yml up -d

# Traefik mode with admin console
docker compose -f docker-compose-single-traefik.yml -f docker-compose-admin-console.yml up -d
```

The admin console **replaces** the built-in console on the same endpoint (`EXPOSED_CONSOLE_PORT` in single mode, `S3_CONSOLE_HOSTNAME` in Traefik mode). You always have either the built-in object browser or the full admin console, never both.

> **Note:** MinIO removed its built-in admin UI in [RELEASE.2025-05-24T17-08-30Z](https://github.com/minio/minio/releases/tag/RELEASE.2025-05-24T17-08-30Z). The built-in console is now an object browser only. The admin console override provides full management capabilities (users, policies, buckets, monitoring).

## Configuration

### Environment Variables

All configuration is done via `.env`. See [.env.example](.env.example) for the full reference with documentation.

**Required settings:**

| Variable | Description |
|----------|-------------|
| `MINIO_ROOT_PASSWORD` | MinIO admin password |
| `APP_SERVICE_SECRET_KEY` | Application service account secret |

**Generate secrets:**
```bash
openssl rand -base64 24
```

### Init Container (JSON Configuration)

The init container reads `config/minio-init.json` and applies the configuration to MinIO on every start. All operations are idempotent.

**Supported resources:**

| Resource | Description |
|----------|-------------|
| `buckets` | Create buckets with versioning, quotas, retention, anonymous policy |
| `policies` | Custom IAM policy documents (S3 policy JSON) |
| `groups` | Groups with policy attachments |
| `users` | Users with group membership and direct policies |
| `service_accounts` | Service accounts with explicit access key / secret key |

**Environment variable resolution:** JSON values support `${VAR_NAME}` syntax. Variables are resolved from the container's environment at runtime, keeping secrets out of config files.

**Example configuration:**
```json
{
  "buckets": [
    {
      "name": "documents",
      "region": "eu-central-1",
      "versioning": true,
      "policy": "private"
    }
  ],
  "policies": [
    {
      "name": "readwrite-documents",
      "statements": [
        {
          "Effect": "Allow",
          "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
          "Resource": ["arn:aws:s3:::documents", "arn:aws:s3:::documents/*"]
        }
      ]
    }
  ],
  "users": [
    {
      "access_key": "app-service",
      "secret_key": "${APP_SERVICE_SECRET_KEY}",
      "groups": [],
      "policies": ["readwrite-documents"]
    }
  ]
}
```

See [src/minio-init/README.md](src/minio-init/README.md) for the full JSON schema reference.

## Custom Docker Images

This repository builds two Docker images:

| Image | Base | Purpose |
|-------|------|---------|
| `ghcr.io/bauer-group/cs-minio/minio` | `quay.io/minio/minio` | MinIO server with health check and OCI labels |
| `ghcr.io/bauer-group/cs-minio/minio-init` | `python:3.14-alpine` | Init container with mc client and task framework |

The server image currently uses the base image as-is. The Dockerfile provides a customization section for adding certificates, scripts, or configurations as needed.

## Traefik Integration

The Traefik deployment mode provides:

- **Automatic HTTPS** via Let's Encrypt certificate resolver
- **S3 API endpoint** on `${S3_HOSTNAME}` (path-style access)
- **Console endpoint** on `${S3_CONSOLE_HOSTNAME}` (object browser or admin console)
- **No-buffering middleware** for large S3 uploads
- **HTTP to HTTPS redirect** on all endpoints

### DNS-Style Bucket Access

Virtual-host-style bucket access (e.g., `bucket.s3.example.com`) is prepared but commented out in the Traefik compose file. To enable it:

1. Configure wildcard certificates (requires DNS challenge) or explicit SANs
2. Uncomment the `HostRegexp` rules in `docker-compose-single-traefik.yml`
3. Set `MINIO_DOMAIN` (already configured in the template)

## Project Structure

```
.
├── .github/                        # GitHub CI/CD configuration
│   ├── CODEOWNERS                  # Pull request review assignments
│   ├── dependabot.yml              # Automated dependency updates
│   ├── config/
│   │   ├── release/                # Semantic release configuration
│   │   └── docker-base-image-monitor/  # Base image monitoring
│   └── workflows/
│       ├── docker-release.yml      # Build, release, push images
│       ├── docker-maintenance.yml  # Auto-merge Dependabot PRs
│       ├── check-base-images.yml   # Daily base image update check
│       ├── teams-notifications.yml # Microsoft Teams notifications
│       └── ai-issue-summary.yml    # AI-powered issue summaries
├── src/
│   ├── minio/                      # MinIO server image
│   │   ├── Dockerfile              # Server image definition
│   │   └── .dockerignore
│   └── minio-init/                 # Init container image
│       ├── Dockerfile              # Init image definition
│       ├── main.py                 # Orchestrator (task discovery, config loading)
│       ├── README.md               # Init container documentation
│       ├── config/
│       │   └── default.json        # Empty default configuration
│       └── tasks/
│           ├── 01_buckets.py       # Bucket creation and configuration
│           ├── 02_policies.py      # IAM policy management
│           ├── 03_groups.py        # Group creation and policy attachment
│           ├── 04_users.py         # User creation and group assignment
│           └── 05_service_accounts.py  # Service account provisioning
├── config/
│   └── minio-init.json             # Init container configuration (user-facing)
├── docker-compose-single.yml       # Single server, direct port access
├── docker-compose-single-traefik.yml  # Single server, Traefik HTTPS
├── docker-compose-admin-console.yml   # Admin console override (append to any mode)
├── .env.example                    # Environment configuration template
├── CHANGELOG.md                    # Release history (auto-generated)
├── LICENSE                         # MIT License
└── README.md                       # This file
```

## CI/CD

The repository uses [semantic-release](https://github.com/semantic-release/semantic-release) with [Conventional Commits](https://www.conventionalcommits.org/):

| Commit Prefix | Version Bump | Example |
|--------------|--------------|---------|
| `fix:` | Patch (0.1.0 -> 0.1.1) | `fix: correct health check endpoint` |
| `feat:` | Minor (0.1.0 -> 0.2.0) | `feat: add bucket lifecycle rules` |
| `BREAKING CHANGE:` | Major (0.1.0 -> 1.0.0) | `feat!: change init config format` |

**Automated pipeline:**

1. Push to `main` triggers validation (compose files)
2. Semantic release creates version tag and GitHub release
3. Docker images are built and pushed to GHCR and Docker Hub
4. Dependabot monitors base images weekly; auto-merges updates
5. Daily base image monitor checks for new releases

## Requirements

- Docker Engine 24.0+
- Docker Compose v2.24+ (required for `!override` tag in admin console override)
- 512 MB RAM minimum (1 GB+ recommended)
- For Traefik mode: Traefik v2/v3 reverse proxy on the `${PROXY_NETWORK}` network

## License

[MIT License](LICENSE) - BAUER GROUP

## Maintainer

Karl Bauer - [karl.bauer@bauer-group.com](mailto:karl.bauer@bauer-group.com)
