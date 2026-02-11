# MinIO Init Container

One-shot initialization container that declaratively configures a MinIO server from a JSON configuration file. Runs on every start and is fully idempotent.

## Features

- **Buckets**: Create with versioning, quotas, retention policies, anonymous access
- **IAM Policies**: Custom S3 policy documents
- **Groups**: Create groups and attach policies
- **Users**: Create users with group membership and direct policies
- **Service Accounts**: Provision with explicit access key / secret key
- **Environment Variable Resolution**: `${VAR_NAME}` syntax in JSON values
- **Task Discovery**: Pluggable task system via numbered Python files

## JSON Configuration Schema

```json
{
  "buckets": [
    {
      "name": "my-bucket",
      "region": "us-east-1",
      "versioning": true,
      "quota": { "type": "hard", "size": "10GB" },
      "retention": { "mode": "compliance", "days": 365 },
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
  "groups": [
    {
      "name": "app-services",
      "policies": ["readwrite-my-bucket"]
    }
  ],
  "users": [
    {
      "access_key": "app-service",
      "secret_key": "${APP_SERVICE_SECRET_KEY}",
      "groups": ["app-services"],
      "policies": []
    }
  ],
  "service_accounts": [
    {
      "user": "app-service",
      "access_key": "sa-access-key",
      "secret_key": "${SA_SECRET_KEY}",
      "name": "My Service Account",
      "description": "Used by application X"
    }
  ]
}
```

## Task Reference

| Order | Task | Config Key | Description |
|-------|------|------------|-------------|
| 01 | Buckets | `buckets` | Create buckets with versioning, quotas, retention |
| 02 | Policies | `policies` | Create custom IAM policy documents |
| 03 | Groups | `groups` | Create groups and attach policies |
| 04 | Users | `users` | Create users, assign groups and policies |
| 05 | Service Accounts | `service_accounts` | Create service accounts with explicit credentials |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `MINIO_ENDPOINT` | `http://minio-server:9000` | MinIO server URL |
| `MINIO_ROOT_USER` | `admin` | MinIO root username |
| `MINIO_ROOT_PASSWORD` | *(required)* | MinIO root password |
| `MINIO_INIT_CONFIG` | `/app/config/init.json` | Path to JSON config file |
| `MINIO_WAIT_TIMEOUT` | `60` | Seconds to wait for MinIO server |

## Adding New Tasks

1. Create a new file in `tasks/` with a numeric prefix (e.g., `06_notifications.py`)
2. Define module-level constants:
   - `TASK_NAME`: Display name
   - `TASK_DESCRIPTION`: Brief description
   - `CONFIG_KEY`: Key in the JSON config to read
3. Implement `run(items: list, console) -> dict`
4. Return `{"changed": bool, "skipped": bool, "message": str}`

## License

MIT License - BAUER GROUP
