"""Handler: MinIO S3 bucket-notification events -> CDN purge jobs.

SOURCE 'minio' -> reached via POST /webhook (default) or /webhook/minio. Purges on
BOTH object-create and object-delete (a delete must stop the edge from serving the
removed/stale object). Pure translation: no I/O, returns durable job dicts. The base
URL is resolved per-provider later (in the worker), so Cloudflare and Bunny can front
different hostnames.
"""

from urllib.parse import quote, unquote_plus

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
    """Translate a MinIO event body into purge jobs (one per changed object).

    Each job carries the object path + bucket + the providers to purge; the receiver
    enqueues one Huey task per (job, provider). Retry state lives in Huey, not here.
    """
    jobs = []
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
        # MinIO encodes the key with Go's url.QueryEscape (escape=true for targets):
        # space -> '+', '/' -> '%2F'. unquote_plus decodes '+' correctly (unquote would
        # not), so re-encoding below yields the exact '%20' URL the edge cached.
        key = unquote_plus(raw_key)
        jobs.append({
            "path": f"{bucket}/{quote(key)}",
            "bucket": bucket,
            "providers": list(ctx.enabled_providers),
        })
    return jobs
