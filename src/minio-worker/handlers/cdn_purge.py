"""Handler: MinIO S3 bucket-notification events -> CDN purge jobs.

SOURCE 'minio' -> reached via POST /webhook (default) or /webhook/minio. Purges on
BOTH object-create and object-delete (a delete must stop the edge from serving the
removed/stale object). Pure translation: no I/O, returns durable job dicts. The base
URL is resolved per-provider later (in the worker), so Cloudflare and Bunny can front
different hostnames.
"""

import time
from urllib.parse import quote, unquote

SOURCE = "minio"

_CREATE = "s3:ObjectCreated"
_REMOVE = "s3:ObjectRemoved"


def _records(payload):
    if isinstance(payload, dict):
        if isinstance(payload.get("Records"), list):
            return payload["Records"]
        if payload.get("EventName") or payload.get("eventName"):
            return [payload]
    return []


def handle(payload, ctx) -> list:
    jobs = []
    now = time.time()
    for rec in _records(payload):
        if not isinstance(rec, dict):
            continue
        event = rec.get("eventName") or rec.get("EventName") or ""
        if not (event.startswith(_CREATE) or event.startswith(_REMOVE)):
            continue
        s3 = rec.get("s3") or {}
        bucket = (s3.get("bucket") or {}).get("name")
        raw_key = (s3.get("object") or {}).get("key")
        if not bucket or not raw_key:
            continue
        key = unquote(raw_key)  # MinIO sends the object key URL-encoded
        jobs.append({
            "action": "purge_url",
            "path": f"{bucket}/{quote(key)}",
            "bucket": bucket,
            "providers": list(ctx.enabled_providers),
            "attempts": 0,
            "next_try_ts": 0.0,
            "created_ts": now,
        })
    return jobs
