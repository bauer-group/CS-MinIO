# MinIO AIStor - Licensed Successor

MinIO has transitioned its enterprise offering to **AIStor**, a licensed object storage
platform built on the same S3-compatible foundation. AIStor requires a valid license file
to operate — there is no unlicensed mode.

> **Reference:** [AIStor Container Installation](https://docs.min.io/enterprise/aistor-object-store/installation/container/install/)

## What Changed

| | MinIO (Community) | AIStor |
|---|---|---|
| **License** | AGPLv3 (open source) | Commercial (license file required) |
| **Image** | `quay.io/minio/minio` | `quay.io/minio/aistor/minio` |
| **S3 API** | Yes | Yes |
| **Iceberg Tables** | No | Native support |
| **SFTP Protocol** | No | Native support |
| **Support** | Community only | Paid tiers available |
| **Free Tier** | N/A | Single compute resource, no support |

## License

A license file is **mandatory** for AIStor. Options:

1. **Free Tier** — single compute resource with one or more drives, no support included.
   Request at [min.io/pricing](https://min.io/pricing) → "Get Started".
2. **Paid Tiers** — commercial support and multi-node deployments.
   See [min.io/pricing](https://min.io/pricing) for details.

Download the license file to a persistent location (e.g. `./minio.license`).

## Container Deployment

```bash
# Pull the AIStor image
docker pull quay.io/minio/aistor/minio

# Create data and certificate directories
mkdir -p ./minio/data ./minio/certs

# Run with license file
docker run -dt \
  -p 9000:9000 -p 9001:9001 \
  -v ./minio/data:/mnt/data \
  -v ./minio.license:/minio.license:ro \
  -v ./minio/certs:/etc/minio/certs \
  --name "aistor-server" \
  quay.io/minio/aistor/minio:latest \
  minio server /mnt/data --license /minio.license
```

Default credentials: `minioadmin` / `minioadmin`

- S3 API: `http://localhost:9000`
- Console: `http://localhost:9001`

## Impact on This Repository

This repository builds MinIO **Community Edition** from source (`RELEASE.2025-10-15T17-29-55Z`)
using our fork. The community edition remains AGPLv3 licensed and does not require a license file.

To migrate to AIStor:

1. Replace `image` in docker-compose with `quay.io/minio/aistor/minio`
2. Remove the source build stage (no longer needed)
3. Mount a valid license file into the container
4. Add `--license /minio.license` to the server command
