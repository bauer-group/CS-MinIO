# MinIO Init Container

One-shot initialization container that declaratively configures a MinIO server from JSON configuration files. Runs on every start and is fully idempotent.

The `mc` client is built from source ([karlspace/MinIO-CLI](https://github.com/karlspace/MinIO-CLI)) and runs on a Python Alpine runtime.

## Configuration Loading

The init container processes two configuration files in order:

1. **Built-in default** (`/app/config/default.json`, baked into image) - Always processed first. Creates the `pAdministrators` policy, `gAdministrators` group, and a console user with full admin rights from `CONSOLE_USER`/`CONSOLE_PASSWORD` environment variables.
2. **User config** (optional) - Loaded from:
   - `MINIO_INIT_CONFIG` environment variable (if set and file exists)
   - `/app/config/init.json` (fallback, if mounted)

Both configs are processed independently through all tasks. Idempotency ensures no conflicts when the same resources appear in both.

## Features

- **Buckets**: Create with versioning, object-lock/WORM, quotas, retention, lifecycle rules, anonymous access
- **IAM Policies**: Create or update custom S3 policy documents
- **Users**: Create users with group membership and direct policies
- **Groups**: Attach policies to groups (a group exists once its first user was added to it)
- **Service Accounts**: Dynamic server-generated credentials, output as JSON files
- **Notifications**: Webhook notification targets and bucket/event bindings (e.g. for CDN cache purge)
- **Environment Variable Resolution**: `${VAR_NAME}` syntax in JSON values
- **Truthful Exit Status**: Exits 1 if any configured item fails; optional items are skipped (see [Exit Status](#exit-status))
- **Task Discovery**: Pluggable task system via numbered Python files

## Exit Status

The init container exits **0** only when every configured item was applied, was already in place, or is an optional item that was intentionally skipped. If any item cannot be applied it exits **1** - after it has still attempted all remaining items, so one run lists every problem. Services that wait for it with `depends_on: { minio-init: { condition: service_completed_successfully } }` therefore never start against a half-applied configuration.

Every item outcome is one log line:

| Log line     | Meaning                                    | Exit status |
| ------------ | ------------------------------------------ | ----------- |
| `Failed: …`  | The item could not be applied              | 1           |
| `Skipped: …` | An optional item was intentionally left out | 0           |
| `Warning: …` | Worth reading, but nothing failed          | 0           |

The summary line ends either in `Initialization complete (…)` or in `Initialization had errors (N failed, …)`.

**Failures (exit 1)**, for example:

- a bucket that cannot be created, or versioning, quota, retention, a lifecycle rule or the anonymous policy that cannot be applied to it
- a policy that cannot be created or updated
- a user MinIO rejects (e.g. a secret shorter than 8 characters), a group membership or a policy attachment that fails (e.g. the policy does not exist)
- a service account that cannot be created, whose scoped `policy` cannot be read, or whose generated credentials cannot be read back
- an invalid notification `id`, a target MinIO rejects although its endpoint is reachable, MinIO not healthy again after the restart, a target still inactive after it, or an event binding that cannot be set
- a `${VAR}` placeholder whose variable is not set at all, and a task module that cannot be loaded

**Skipped (exit 0)** - optional items:

| Item            | Skipped when                                                                                                                                                                                                                  |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| User            | `access_key` or `secret_key` is an empty string, e.g. `"${BACKUP_PASSWORD}"` with `BACKUP_PASSWORD=` set but empty (Compose also passes an unset variable as empty). Also the root user (`MINIO_ROOT_USER`), which is not an IAM user |
| Group           | It has no `policies`, or no members: MinIO only knows a group once a user was added to it, so a group whose users were all skipped is skipped too                                                                            |
| Service account | Its parent `user` was skipped or is an empty string                                                                                                                                                                           |
| Notification    | It has no `endpoint`, or MinIO refuses the target because the endpoint is not reachable (the receiver, e.g. the opt-in `minio-worker`, is not running). Its bindings are skipped with it; the next start registers it          |

**Warnings (exit 0):** `object_lock` requested for a bucket that already exists, invalid `cors` rules, and an unknown bucket `policy` value (the bucket's anonymous access is then left unchanged).

**Upgrading from a version that always exited 0:** earlier versions logged most of these failures but still exited 0, so a misconfiguration could go unnoticed while dependent services started anyway. If `minio-init` now exits 1, `docker compose logs minio-init` names every failed item. Fix the configuration (for example a secret shorter than 8 characters or a misspelled policy name) and run `docker compose up -d` again.

## JSON Configuration Schema

```json
{
  "buckets": [
    {
      "name": "my-bucket",
      "region": "eu-central-1",
      "versioning": true,
      "object_lock": true,
      "quota": { "type": "hard", "size": "10GB" },
      "retention": { "mode": "compliance", "days": 365 },
      "lifecycle_rules": [
        { "prefix": "daily/", "expire_days": 15 },
        { "prefix": "archive/", "expire_days": 365, "noncurrent_expire_days": 30 }
      ],
      "policy": "private"
    }
  ],
  "policies": [
    {
      "name": "readwrite-my-bucket",
      "statements": [
        {
          "Effect": "Allow",
          "Action": [
            "s3:GetObject",
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:ListBucket",
            "s3:GetBucketLocation"
          ],
          "Resource": [
            "arn:aws:s3:::my-bucket",
            "arn:aws:s3:::my-bucket/*"
          ]
        }
      ]
    }
  ],
  "users": [
    {
      "access_key": "${APP_USER}",
      "secret_key": "${APP_PASSWORD}",
      "groups": ["app-services"],
      "policies": []
    }
  ],
  "groups": [
    {
      "name": "app-services",
      "policies": ["readwrite-my-bucket"]
    }
  ],
  "service_accounts": [
    {
      "user": "${APP_USER}",
      "name": "my-service-account",
      "description": "Used by application X",
      "policy": "readwrite-my-bucket"
    }
  ]
}
```

### Bucket Options

| Field         | Type    | Default      | Description                                            |
| ------------- | ------- | ------------ | ------------------------------------------------------ |
| `name`        | string  | *(required)* | Bucket name                                            |
| `region`      | string  | `""`         | Bucket region                                          |
| `versioning`  | boolean | `false`      | Enable versioning (implied by `object_lock`)           |
| `object_lock` | boolean | `false`      | Enable object-lock/WORM (must be set at creation time) |
| `quota`       | object  | -            | `{"type": "hard", "size": "10GB"}`                     |
| `retention`       | object  | -            | `{"mode": "compliance", "days": 365}` (requires lock)  |
| `lifecycle_rules` | array   | `[]`         | Prefix-based expiration rules (see below)              |
| `policy`          | string  | `"private"`  | `private`, `public` (download), `public-readwrite`; any other value is a warning and leaves anonymous access unchanged |
| `cors`            | array   | `[]`         | S3-compatible per-bucket CORS rules (see below)        |

**Retention validity:** Specify either `days` or `years` in the retention object. The init container converts these to the `mc retention set` format (`365d` or `1y`).

**Lifecycle rules:** Each rule in the `lifecycle_rules` array supports:

| Field                    | Type    | Required | Default | Description                              |
| ------------------------ | ------- | -------- | ------- | ---------------------------------------- |
| `prefix`                 | string  | No       | `""`    | Object prefix filter (empty = all)       |
| `expire_days`            | integer | No*      | -       | Expire current versions after N days     |
| `noncurrent_expire_days` | integer | No       | -       | Expire noncurrent versions after N days  |
| `expire_delete_marker`   | boolean | No       | `false` | Remove expired delete markers            |

\*At least one of `expire_days` or `noncurrent_expire_days` is required.

Lifecycle rules are matched by prefix for idempotency. On re-run, existing rules with the same prefix are updated if settings differ, or left unchanged if already correct. Extra copies of a configured rule are removed - earlier versions could not read `mc ilm rule ls` and added every rule again on each start, so long-running buckets may hold many identical rules that the first run of this version cleans up. Rules whose prefix is not in the config are not removed (additive-only).

**CORS rules:** Each rule in the `cors` array is an S3-compatible CORS rule:

| Field             | Type    | Required | Default | Description                                       |
| ----------------- | ------- | -------- | ------- | ------------------------------------------------- |
| `allowed_origins` | array   | Yes      | -       | Allowed origins (S3 `AllowedOrigins`)             |
| `allowed_methods` | array   | Yes      | -       | Subset of `GET`, `PUT`, `POST`, `DELETE`, `HEAD`  |
| `allowed_headers` | array   | No       | `["*"]` | Allowed request headers (S3 `AllowedHeaders`)     |
| `expose_headers`  | array   | No       | -       | Response headers exposed to the browser           |
| `max_age_seconds` | integer | No       | -       | Preflight cache duration (S3 `MaxAgeSeconds`)     |

> **Engine-dependent — no-op on MinIO.** Open-source MinIO has no per-bucket CORS API and
> enforces CORS **globally** via the `MINIO_API_CORS_ALLOW_ORIGIN` server setting (default `*`,
> which already allows presigned browser uploads). The init container **validates** `cors` but
> does **not** apply it on MinIO. The field exists so applications can declare CORS today; a
> storage engine with per-bucket CORS (e.g. SeaweedFS via `PutBucketCors`) can apply the same
> config later without any application change. Invalid rules produce a warning but do not fail
> the run.

**Existing bucket limitations:**

| Setting           | New Bucket | Existing Bucket                    |
| ----------------- | ---------- | ---------------------------------- |
| `object_lock`     | Applied    | Ignored (immutable after creation) |
| `versioning`      | Applied    | Applied                            |
| `quota`           | Applied    | Updated                            |
| `retention`       | Applied    | Updated                            |
| `lifecycle_rules` | Applied    | Updated (per-prefix idempotent)    |
| `policy`          | Applied    | Updated                            |
| `cors`            | Declared   | No-op on MinIO (engine-dependent)  |

### Service Account Options

| Field         | Type   | Default      | Description                                     |
| ------------- | ------ | ------------ | ----------------------------------------------- |
| `user`        | string | *(required)* | Parent user for the service account             |
| `name`        | string | `sa-{user}`  | Display name (used as filename for credentials) |
| `description` | string | `""`         | Description                                     |
| `policy`      | string | -            | Named policy the account is restricted to; without it the account inherits the parent user's permissions. A policy that cannot be read fails the run |

Credentials are generated dynamically by MinIO and written to `/data/credentials/<name>.json`:

```json
{
  "accessKey": "generated-access-key",
  "secretKey": "generated-secret-key",
  "user": "parent-user",
  "name": "my-service-account"
}
```

**Re-runs:** the account is kept as long as `<name>.json` still holds an access key that MinIO lists for the parent user - nothing is created and the file stays as it is. If the file is missing or its account was deleted, a new account is created and the file rewritten, so keep `/data/credentials` on a persistent volume. Earlier versions created a new account (and rewrote the file) on every start; those older accounts stay valid until they are removed - list them with `mc admin user svcacct ls <alias> <user>` and remove every access key except the one in `<name>.json` with `mc admin user svcacct rm <alias> <access-key>`.

### Bucket Notifications

Forward object events (create/delete) to a webhook target - used by the optional
`minio-worker` relay for CDN cache purging. Each entry co-locates the webhook target
and its bucket/event bindings.

```json
{
  "notifications": [
    {
      "id": "cdnpurge",
      "type": "webhook",
      "endpoint": "http://minio-worker:8080/webhook",
      "auth_token": "${WEBHOOK_AUTH_TOKEN}",
      "queue_dir": "/data/.minio-events",
      "queue_limit": 100000,
      "buckets": ["*"],
      "events": ["put", "delete"],
      "prefix": "",
      "suffix": ""
    }
  ]
}
```

| Field         | Type     | Default            | Description                                                             |
| ------------- | -------- | ------------------ | ----------------------------------------------------------------------- |
| `id`          | string   | *(required)*       | Target id (`[A-Za-z0-9_-]`) -> ARN `arn:minio:sqs::<id>:webhook`        |
| `type`        | string   | `"webhook"`        | Only `webhook` is supported                                             |
| `endpoint`    | string   | *(required)*       | Webhook URL reachable from the MinIO container                          |
| `auth_token`  | string   | `""`               | Shared secret sent as `Authorization` (use `${WEBHOOK_AUTH_TOKEN}`)     |
| `queue_dir`   | string   | `""`               | Server-side store-and-forward dir on a MinIO volume (recommended)       |
| `queue_limit` | number   | `100000`           | Max queued events                                                       |
| `buckets`     | string[] | `["*"]`            | `["*"]` = all buckets (resolved at run time), or an explicit list       |
| `events`      | string[] | `["put","delete"]` | Short `mc event` names: `put`, `delete`, `get`, `replica`               |
| `prefix`      | string   | `""`               | Optional object-key prefix filter                                       |
| `suffix`      | string   | `""`               | Optional object-key suffix filter                                       |

**Granularity:** target a single bucket (`["iam"]`), all buckets (`["*"]`), or narrow a
bucket with `prefix`/`suffix`. For different filters per bucket, use multiple entries.

**Restart behavior:** registering or changing a webhook *target* requires a one-time MinIO
restart, which the task performs automatically and then waits for health. It restarts
**only when the target config actually changed** (tracked via a hash marker under
`/data/credentials/.notifications/`), so re-running with unchanged config causes no
restart. Event *bindings* never require a restart and are additive (not removed).

**Bundled example:** [`config/minio-init.example.json`](../../config/minio-init.example.json)
ships a ready-to-use `cdnpurge` entry (scoped to the `public-assets` bucket).

**Prerequisite:** the `minio-worker` container must be enabled (Compose profile `worker`)
and given CDN credentials to receive and act on these webhooks. See
[src/minio-worker/README.md](../minio-worker/README.md). While the endpoint is not
reachable (worker disabled or not started yet), MinIO refuses to register the target: the
entry and its bindings are reported as skipped (exit 0) and registered on the next start.
A target MinIO rejects although its endpoint is reachable is a failure (exit 1).

## Task Reference

| Order | Task             | Config Key         | Description                                             |
| ----- | ---------------- | ------------------ | ------------------------------------------------------- |
| 01    | Buckets          | `buckets`          | Create buckets with versioning, lock, quotas, lifecycle |
| 02    | Policies         | `policies`         | Create or update IAM policy documents                   |
| 03    | Users            | `users`            | Create users, assign to groups, attach policies         |
| 04    | Groups           | `groups`           | Attach policies to groups                               |
| 05    | Service Accounts | `service_accounts` | Create service accounts with dynamic credentials        |
| 06    | Notifications    | `notifications`    | Configure webhook targets and bucket/event bindings     |

> **Note:** Users (03) run before groups (04). Groups are implicitly created when users are added via `mc admin group add`. The groups task then attaches policies via `mc admin policy attach --group`. This ordering ensures policy attachments persist (group membership updates cannot overwrite them). A group nobody was added to does not exist in MinIO, so its policies cannot be attached: it is reported as skipped.

> **Note:** The init container is additive only - it creates and updates resources but does not remove them. To delete buckets, policies, users, or groups, use the admin console or `mc` CLI directly.

## Environment Variables

| Variable                | Default                    | Description                         |
| ----------------------- | -------------------------- | ----------------------------------- |
| `MINIO_ENDPOINT`        | `http://minio-server:9000` | MinIO server URL                    |
| `MINIO_ROOT_USER`       | `minioadmin`               | MinIO root username                 |
| `MINIO_ROOT_PASSWORD`   | `minioadmin`               | MinIO root password                 |
| `MINIO_INIT_CONFIG`     | `/app/config/init.json`    | Path to user JSON config file       |
| `MINIO_WAIT_TIMEOUT`    | `60`                       | Seconds to wait for MinIO server    |
| `MINIO_CREDENTIALS_DIR` | `/data/credentials`        | Output directory for SA credentials |

`NOTIFY_MARKER_DIR` (default `/data/credentials/.notifications`) controls where the
notifications task stores target-config hashes used to decide when a MinIO restart is
required.

## Adding New Tasks

1. Create a new file in `tasks/` with a numeric prefix (e.g., `07_myfeature.py`)
2. Define module-level constants:
   - `TASK_NAME`: Display name
   - `TASK_DESCRIPTION`: Brief description
   - `CONFIG_KEY`: Key in the JSON config to read
3. Implement `run(items: list, console, **kwargs) -> dict`. `kwargs["context"]` is a dict
   shared by all tasks and configs of one run (e.g. `skipped_users`).
4. Return `{"changed": bool, "skipped": bool, "message": str, "failed": int, "items_skipped": int}`.
   `failed` > 0 makes the run exit 1 (see [Exit Status](#exit-status)). Run mc through
   `mc()` and report items with `fail()`, `skip()` and `warn()` from `tasks/_mc.py`, which
   is not a task itself (a leading underscore excludes a module from discovery).

## License

MIT License - BAUER GROUP
