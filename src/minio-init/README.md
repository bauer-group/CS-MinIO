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
- **Groups**: Attach policies to groups (groups are created implicitly when policies are attached)
- **Service Accounts**: Dynamic server-generated credentials, output as JSON files
- **Environment Variable Resolution**: `${VAR_NAME}` syntax in JSON values
- **Task Discovery**: Pluggable task system via numbered Python files

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
| `policy`          | string  | `"private"`  | `private`, `public` (download), `public-readwrite`     |

**Retention validity:** Specify either `days` or `years` in the retention object. The init container converts these to the `mc retention set` format (`365d` or `1y`).

**Lifecycle rules:** Each rule in the `lifecycle_rules` array supports:

| Field                    | Type    | Required | Default | Description                              |
| ------------------------ | ------- | -------- | ------- | ---------------------------------------- |
| `prefix`                 | string  | No       | `""`    | Object prefix filter (empty = all)       |
| `expire_days`            | integer | No*      | -       | Expire current versions after N days     |
| `noncurrent_expire_days` | integer | No       | -       | Expire noncurrent versions after N days  |
| `expire_delete_marker`   | boolean | No       | `false` | Remove expired delete markers            |

\*At least one of `expire_days` or `noncurrent_expire_days` is required.

Lifecycle rules are matched by prefix for idempotency. On re-run, existing rules with the same prefix are updated if settings differ, or left unchanged if already correct. Rules not present in the config are not removed (additive-only).

**Existing bucket limitations:**

| Setting           | New Bucket | Existing Bucket                    |
| ----------------- | ---------- | ---------------------------------- |
| `object_lock`     | Applied    | Ignored (immutable after creation) |
| `versioning`      | Applied    | Applied                            |
| `quota`           | Applied    | Updated                            |
| `retention`       | Applied    | Updated                            |
| `lifecycle_rules` | Applied    | Updated (per-prefix idempotent)    |
| `policy`          | Applied    | Updated                            |

### Service Account Options

| Field         | Type   | Default      | Description                                     |
| ------------- | ------ | ------------ | ----------------------------------------------- |
| `user`        | string | *(required)* | Parent user for the service account             |
| `name`        | string | `sa-{user}`  | Display name (used as filename for credentials) |
| `description` | string | `""`         | Description                                     |
| `policy`      | string | -            | Named policy to scope the service account       |

Credentials are generated dynamically by MinIO and written to `/data/credentials/<name>.json`:

```json
{
  "accessKey": "generated-access-key",
  "secretKey": "generated-secret-key",
  "user": "parent-user",
  "name": "my-service-account"
}
```

## Task Reference

| Order | Task             | Config Key         | Description                                             |
| ----- | ---------------- | ------------------ | ------------------------------------------------------- |
| 01    | Buckets          | `buckets`          | Create buckets with versioning, lock, quotas, lifecycle |
| 02    | Policies         | `policies`         | Create or update IAM policy documents                   |
| 03    | Users            | `users`            | Create users, assign to groups, attach policies         |
| 04    | Groups           | `groups`           | Attach policies to groups                               |
| 05    | Service Accounts | `service_accounts` | Create service accounts with dynamic credentials        |

> **Note:** Users (03) run before groups (04). Groups are implicitly created when users are added via `mc admin group add`. The groups task then attaches policies via `mc admin policy attach --group`. This ordering ensures policy attachments persist (group membership updates cannot overwrite them).

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

## Adding New Tasks

1. Create a new file in `tasks/` with a numeric prefix (e.g., `06_notifications.py`)
2. Define module-level constants:
   - `TASK_NAME`: Display name
   - `TASK_DESCRIPTION`: Brief description
   - `CONFIG_KEY`: Key in the JSON config to read
3. Implement `run(items: list, console, **kwargs) -> dict`
4. Return `{"changed": bool, "skipped": bool, "message": str}`

## License

MIT License - BAUER GROUP
