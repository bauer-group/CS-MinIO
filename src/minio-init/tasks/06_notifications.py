"""
Bucket Notifications Task

Configures MinIO webhook notification targets and their bucket/event bindings so
object changes can be forwarded to the minio-worker relay (e.g. for CDN cache purge).

Everything is declared in one place. Each entry co-locates the webhook *target*
(endpoint, auth token, server-side queue) and its *bindings* (which buckets, which
events, optional prefix/suffix filter):

JSON config example:
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

Granularity: `buckets` may be ["*"] (all buckets, resolved at run time), an explicit
list (["iam"]), and can be narrowed with `prefix`/`suffix` object-key filters. For
different filters per bucket, use multiple notification entries.

Idempotency (critical):
  - Registering/altering a notify_webhook TARGET requires `mc admin service restart`.
    MinIO masks the auth_token in `mc admin config get`, so we cannot diff it directly.
    Instead we persist a hash of the desired target config to a marker file on the
    credentials volume and restart ONLY when that hash changes AND when the target is
    actually present on the server. Running init twice unchanged -> no restart.
  - Event BINDINGS never require a restart. `mc event add` uses short event names
    (put/delete) but `mc event ls` reports full names (s3:ObjectCreated:*), so we map
    short -> full before comparing to avoid re-adding an existing binding.
  - Additive only: bindings not present in the config are left untouched (like lifecycle).
"""

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

TASK_NAME = "Notifications"
TASK_DESCRIPTION = "Configure bucket notification targets and event bindings"
CONFIG_KEY = "notifications"

MC_ALIAS = "minio"
MARKER_DIR = os.environ.get("NOTIFY_MARKER_DIR", "/data/credentials/.notifications")

# mc event add takes short names; mc event ls reports the full S3 event names.
_EVENT_FULL = {
    "put": "s3:ObjectCreated:*",
    "delete": "s3:ObjectRemoved:*",
    "get": "s3:ObjectAccessed:*",
    "replica": "s3:Replication:*",
    "ilm": "s3:ObjectRestore:*",
    "scanner": "s3:Scanner:*",
}


def _mc(args: list, use_json: bool = True) -> subprocess.CompletedProcess:
    cmd = ["mc"] + (["--json"] if use_json else []) + args
    result = subprocess.run(cmd, capture_output=True, text=True)
    # mc --json outputs errors to stdout as JSON, not stderr
    if result.returncode != 0 and not result.stderr.strip():
        for line in (result.stdout or "").splitlines():
            try:
                err = json.loads(line).get("error", {})
                if isinstance(err, dict) and err.get("message"):
                    result.stderr = err["message"]
                    break
                elif isinstance(err, str) and err:
                    result.stderr = err
                    break
            except (json.JSONDecodeError, AttributeError):
                continue
    return result


def _desired_kv(entry: dict) -> dict:
    """Target config keys we manage (order-independent; compared via hash)."""
    return {
        "endpoint": entry["endpoint"],
        "auth_token": entry.get("auth_token", ""),
        "queue_dir": entry.get("queue_dir", ""),
        "queue_limit": str(entry.get("queue_limit", 100000)),
    }


def _hash(target_id: str, kv: dict) -> str:
    canon = json.dumps({"id": target_id, **kv}, sort_keys=True)
    return hashlib.sha256(canon.encode()).hexdigest()


def _marker_path(target_id: str) -> Path:
    return Path(MARKER_DIR) / f"{target_id}.sha256"


def _read_marker(target_id: str) -> str | None:
    try:
        return _marker_path(target_id).read_text().strip()
    except OSError:
        return None


def _write_marker(target_id: str, digest: str, console) -> None:
    try:
        Path(MARKER_DIR).mkdir(parents=True, exist_ok=True)
        _marker_path(target_id).write_text(digest)
    except OSError as e:
        console.print(f"    [yellow]Warning: could not persist marker for {target_id}: {e}[/]")


def _target_exists(target_id: str) -> bool:
    """True if the notify_webhook target is actually present (has an endpoint) on the server.

    Guards against a marker that survived a MinIO data reset (credentials and data are
    separate volumes) - without this, a stale-but-matching marker would skip re-creating
    a target that no longer exists, and the later `mc event add` would fail.
    """
    res = _mc(["admin", "config", "get", MC_ALIAS, f"notify_webhook:{target_id}"], use_json=False)
    if res.returncode != 0:
        return False
    return bool(re.search(r'endpoint="?([^"\s]+)', res.stdout or ""))


def _to_full(events: list) -> set:
    return {_EVENT_FULL.get(e, e) for e in events}


def _list_buckets() -> list:
    result = _mc(["ls", MC_ALIAS])
    buckets = []
    if result.returncode != 0:
        return buckets
    for line in result.stdout.strip().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = data.get("key", "")
        if key:
            buckets.append(key.rstrip("/"))
    return buckets


def _existing_bindings(bucket: str) -> list:
    result = _mc(["event", "ls", f"{MC_ALIAS}/{bucket}"])
    bindings = []
    if result.returncode != 0:
        return bindings
    for line in result.stdout.strip().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        arn = data.get("arn") or data.get("id")
        if not arn:
            continue
        events = data.get("events") or data.get("event") or []
        bindings.append({
            "arn": arn,
            "events": set(events),
            "prefix": data.get("prefix", ""),
            "suffix": data.get("suffix", ""),
        })
    return bindings


def _wait_healthy(timeout: int) -> bool:
    endpoint = os.environ.get("MINIO_ENDPOINT", "http://minio-server:9000")
    time.sleep(2)  # let the restart begin before polling
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = subprocess.run(
                ["curl", "-sf", f"{endpoint}/minio/health/live"],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No notifications configured"}

    restart_required = False
    targets_set = 0
    bindings_added = 0
    skipped = 0
    valid = []

    # --- Phase 1: Targets (may require a single restart) ---
    for entry in items:
        target_id = entry.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", target_id or ""):
            console.print(f"    [yellow]Warning: invalid notification id '{target_id}', skipping[/]")
            skipped += 1
            continue
        if not entry.get("endpoint"):
            console.print(f"    [yellow]Warning: notification '{target_id}' has no endpoint, skipping[/]")
            skipped += 1
            continue

        valid.append(entry)
        kv = _desired_kv(entry)
        digest = _hash(target_id, kv)

        if _read_marker(target_id) == digest and _target_exists(target_id):
            console.print(f"    [dim]Target unchanged: {target_id}[/]")
            continue

        cmd = ["admin", "config", "set", MC_ALIAS, f"notify_webhook:{target_id}"]
        cmd += [f"{k}={v}" for k, v in kv.items()]
        res = _mc(cmd)
        if res.returncode == 0:
            targets_set += 1
            restart_required = True
            _write_marker(target_id, digest, console)
            console.print(f"    [green]Target configured: {target_id}[/]")
        else:
            console.print(f"    [red]Failed to set target {target_id}: {res.stderr.strip()}[/]")

    # --- Restart once if any target changed ---
    if restart_required:
        console.print("    [dim]Restarting MinIO to apply notification target(s)...[/]")
        rr = _mc(["admin", "service", "restart", MC_ALIAS])
        if rr.returncode != 0:
            console.print(f"    [yellow]Warning: service restart returned: {rr.stderr.strip()}[/]")
        timeout = int(os.environ.get("MINIO_WAIT_TIMEOUT", "60"))
        if _wait_healthy(timeout):
            console.print("    [green]MinIO healthy after restart[/]")
        else:
            console.print("    [red]MinIO did not become healthy after restart[/]")
            return {
                "changed": True,
                "message": f"{targets_set} target(s) set, restart health-check timed out",
            }

    # --- Phase 2: Event bindings (no restart, idempotent, additive) ---
    for entry in valid:
        target_id = entry["id"]
        arn = f"arn:minio:sqs::{target_id}:webhook"
        events = entry.get("events", ["put", "delete"])
        prefix = entry.get("prefix", "")
        suffix = entry.get("suffix", "")
        desired_full = _to_full(events)

        buckets = entry.get("buckets", ["*"])
        if buckets == ["*"]:
            buckets = _list_buckets()

        for bucket in buckets:
            match = next((b for b in _existing_bindings(bucket) if b["arn"] == arn), None)
            if match and match["events"] == desired_full \
                    and match["prefix"] == prefix and match["suffix"] == suffix:
                continue
            if match:  # events/filter changed -> replace
                _mc(["event", "remove", f"{MC_ALIAS}/{bucket}", arn])

            cmd = ["event", "add", f"{MC_ALIAS}/{bucket}", arn, "--event", ",".join(events)]
            if prefix:
                cmd += ["--prefix", prefix]
            if suffix:
                cmd += ["--suffix", suffix]
            res = _mc(cmd)

            if res.returncode == 0:
                bindings_added += 1
                filt = f" (prefix='{prefix}', suffix='{suffix}')" if (prefix or suffix) else ""
                console.print(f"    [green]Bound {target_id} -> {bucket}{filt}[/]")
            elif "already exists" in (res.stderr or "").lower():
                console.print(f"    [dim]Binding exists: {target_id} -> {bucket}[/]")
            else:
                console.print(
                    f"    [yellow]Warning: bind {target_id} -> {bucket} failed: {res.stderr.strip()}[/]"
                )

    changed = targets_set > 0 or bindings_added > 0
    msg = (
        f"{targets_set} target(s), {bindings_added} binding(s), "
        f"restart={'yes' if restart_required else 'no'}"
    )
    if skipped:
        msg += f", {skipped} skipped"
    return {"changed": changed, "message": msg}
