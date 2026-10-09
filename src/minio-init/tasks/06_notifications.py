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
    credentials volume. We skip the restart when that hash matches AND the target's ARN is
    already active on the running server (`mc admin info` -> `info.sqsARN`); otherwise we
    (re)apply and restart once. Targets persist across restarts, so steady state is no-op.
  - Event BINDINGS never require a restart. `mc event add` uses short event names
    (put/delete) but `mc event ls` reports full names (s3:ObjectCreated:*), so we map
    short -> full before comparing to avoid re-adding an existing binding.
  - The ARN passed to `mc event add` / compared against `mc event ls` must be the server's
    EXACT string. MinIO qualifies target ARNs with the server region
    (`arn:minio:sqs:<region>:<id>:webhook`), so a hardcoded empty-region ARN fails with
    "Unable to enable notification on the specified bucket" once MINIO_SITE_REGION is set,
    even though the target is registered. We resolve the real ARN from `info.sqsARN`.
  - Additive only: bindings not present in the config are left untouched (like lifecycle).

Failures vs. skips:
  - An entry without an endpoint is skipped. So is a target MinIO refuses to register
    while its endpoint is unreachable from here (MinIO tests the connection when the
    target is set): the receiver - e.g. the opt-in minio-worker - is not running. Its
    bindings are skipped with it; the next start registers it once the endpoint is up.
  - If the endpoint turns out to be reachable after MinIO refused the target, the receiver
    has probably just finished starting next to us: the target is set once more.
  - Everything else is a failure and the init container exits 1: an invalid id, a target
    MinIO rejects although its endpoint is reachable, MinIO not healthy again after the
    restart, a target that is still not active after it, or a binding that cannot be set.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from ._mc import MC_ALIAS, fail, skip, warn
from ._mc import iter_json as _iter_json
from ._mc import mc as _mc

TASK_NAME = "Notifications"
TASK_DESCRIPTION = "Configure bucket notification targets and event bindings"
CONFIG_KEY = "notifications"

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
        warn(console, f"could not persist marker for {target_id}: {e}")


def _target_exists(target_id: str) -> bool:
    """True if the notify_webhook target is present (has an endpoint) in persisted config.

    Fallback only, used when the runtime ARN list is unavailable (see `_active_arns`).
    """
    res = _mc(["admin", "config", "get", MC_ALIAS, f"notify_webhook:{target_id}"], use_json=False)
    if res.returncode != 0:
        return False
    return bool(re.search(r'endpoint="?([^"\s]+)', res.stdout or ""))


def _active_arns() -> set | None:
    """Notification target ARNs the *running* server has loaded, or None if undeterminable.

    Reads `mc admin info`'s `info.sqsARN`. That list reflects runtime state: a
    notify_webhook target set via `config set` does NOT appear until `service restart`,
    and `mc event add` accepts an ARN only once it is here. The field carries `omitempty`,
    so an absent/empty list legitimately means "no active targets" (a valid negative — we
    must NOT fall back to persisted config in that case, or we'd skip the needed restart).
    Returns None only when the admin-info call itself fails.
    """
    res = _mc(["admin", "info", MC_ALIAS])
    if res.returncode != 0:
        return None
    arns = set()
    for data in _iter_json(res.stdout):
        if not isinstance(data, dict):
            continue
        for container in (data, data.get("info", {})):
            if isinstance(container, dict):
                for arn in (container.get("sqsARN") or []):
                    if arn:
                        arns.add(arn)
    return arns


def _find_arn(target_id: str, active: set | None) -> str | None:
    """The server's exact webhook ARN for `target_id`, matched region-agnostically.

    MinIO qualifies notification ARNs with the server region:
    ``arn:minio:sqs:<region>:<id>:webhook`` (empty region when MINIO_SITE_REGION is unset).
    We must feed `mc event add` the server's *exact* string — a hardcoded empty-region ARN
    fails with "Unable to enable notification on the specified bucket" whenever a region is
    configured, even though the target is registered. So we match on the id/type segments
    instead of guessing the region. Returns None if the target isn't in the active list.
    """
    if not active:
        return None
    for arn in active:
        parts = arn.split(":")  # arn : minio : sqs : <region> : <id> : <type>
        if len(parts) == 6 and parts[4] == target_id and parts[5] == "webhook":
            return arn
    return None


def _target_active(target_id: str, active: set | None) -> bool:
    """Whether the target's webhook ARN is loaded on the running server.

    Uses the runtime ARN list when available; falls back to persisted-config presence
    only when `mc admin info` could not be read.
    """
    if active is None:
        return _target_exists(target_id)
    return _find_arn(target_id, active) is not None


def _to_full(events: list) -> set:
    return {_EVENT_FULL.get(e, e) for e in events}


def _list_buckets() -> list | None:
    """All bucket names, or None when they cannot be listed."""
    result = _mc(["ls", MC_ALIAS])
    buckets = []
    if result.returncode != 0:
        return None
    for data in _iter_json(result.stdout):
        if not isinstance(data, dict):
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
    for data in _iter_json(result.stdout):
        if not isinstance(data, dict):
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


def _endpoint_unreachable(endpoint: str, attempts: int = 3, delay: float = 2.0) -> bool:
    """True when the endpoint is a well-formed URL but no TCP connection to it succeeds.

    Used only after MinIO refused a target, to tell "the receiver is not running" (skip)
    from a real misconfiguration (fail). A few attempts cover a receiver that is still
    starting next to us. A malformed endpoint is not "unreachable" - it is a config error.
    """
    try:
        url = urlparse(endpoint)
        host = url.hostname
        port = url.port or (443 if url.scheme == "https" else 80)
    except ValueError:
        return False
    if not host or url.scheme not in ("http", "https"):
        return False
    for attempt in range(attempts):
        try:
            with socket.create_connection((host, port), timeout=3):
                return False
        except OSError:
            if attempt < attempts - 1:
                time.sleep(delay)
    return True


def _host_port(endpoint: str) -> str:
    """host:port of an endpoint URL for log lines.

    Never the whole URL: a webhook URL may carry credentials (user:password@ or a
    token in its query), and init logs end up in CI runs and support tickets.
    """
    try:
        url = urlparse(endpoint)
        return f"{url.hostname}:{url.port or (443 if url.scheme == 'https' else 80)}"
    except ValueError:
        return "(invalid URL)"


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
    failed = 0
    valid = []
    applied = []  # target ids configured in this run (must be active after the restart)

    # --- Phase 1: Targets (may require a single restart) ---
    active = _active_arns()  # runtime ARN list; source of truth for "already active"
    for entry in items:
        target_id = entry.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", target_id or ""):
            fail(console, f"invalid notification id '{target_id}' (allowed: A-Z, a-z, 0-9, _ and -)")
            failed += 1
            continue
        if not entry.get("endpoint"):
            skip(console, f"notification '{target_id}' has no endpoint")
            skipped += 1
            continue

        kv = _desired_kv(entry)
        digest = _hash(target_id, kv)

        if _read_marker(target_id) == digest and _target_active(target_id, active):
            console.print(f"    [dim]Target unchanged: {target_id}[/]")
            valid.append(entry)
            continue

        cmd = ["admin", "config", "set", MC_ALIAS, f"notify_webhook:{target_id}"]
        cmd += [f"{k}={v}" for k, v in kv.items()]
        res = _mc(cmd)
        unreachable = False
        if res.returncode != 0:
            unreachable = _endpoint_unreachable(entry["endpoint"])
            if not unreachable:
                # Reachable now: the receiver may have finished starting while MinIO
                # tested it (it starts next to us). One more attempt decides.
                res = _mc(cmd)
        if res.returncode == 0:
            targets_set += 1
            restart_required = True
            _write_marker(target_id, digest, console)
            console.print(f"    [green]Target configured: {target_id}[/]")
            valid.append(entry)
            applied.append(target_id)
        elif unreachable:
            skip(
                console,
                f"notification '{target_id}': endpoint {_host_port(entry['endpoint'])} is not reachable "
                f"(receiver not running?) - target and its bindings not configured",
            )
            skipped += 1
        else:
            fail(console, f"set notification target {target_id}: {res.stderr}")
            failed += 1

    # --- Restart once if any target changed ---
    if restart_required:
        console.print("    [dim]Restarting MinIO to apply notification target(s)...[/]")
        rr = _mc(["admin", "service", "restart", MC_ALIAS])
        if rr.returncode != 0:
            warn(console, f"service restart returned: {rr.stderr}")
        timeout = int(os.environ.get("MINIO_WAIT_TIMEOUT", "60"))
        if _wait_healthy(timeout):
            console.print("    [green]MinIO healthy after restart[/]")
        else:
            fail(console, f"MinIO did not become healthy within {timeout}s after the restart")
            return {
                "changed": True,
                "message": f"{targets_set} target(s) set, restart health-check timed out",
                "failed": failed + 1,
                "items_skipped": skipped,
            }
        active = _active_arns()  # refresh: newly-applied targets now carry their ARN
        if active is not None:
            for target_id in applied:
                if _find_arn(target_id, active) is None:
                    fail(console, f"notification target {target_id} is not active after the restart")
                    failed += 1
                    valid = [e for e in valid if e["id"] != target_id]

    # --- Phase 2: Event bindings (no restart, idempotent, additive) ---
    for entry in valid:
        target_id = entry["id"]
        # Use the server's exact (region-qualified) ARN; the empty-region fallback only
        # applies if admin-info was unavailable, matching MinIO's default no-region format.
        arn = _find_arn(target_id, active) or f"arn:minio:sqs::{target_id}:webhook"
        events = entry.get("events", ["put", "delete"])
        prefix = entry.get("prefix", "")
        suffix = entry.get("suffix", "")
        desired_full = _to_full(events)

        buckets = entry.get("buckets", ["*"])
        if buckets == ["*"]:
            buckets = _list_buckets()
            if buckets is None:
                fail(console, f"list buckets for notification '{target_id}'")
                failed += 1
                continue

        for bucket in buckets:
            match = next((b for b in _existing_bindings(bucket) if b["arn"] == arn), None)
            if match and match["events"] == desired_full \
                    and match["prefix"] == prefix and match["suffix"] == suffix:
                continue
            if match:  # events/filter changed -> replace
                rm = _mc(["event", "remove", f"{MC_ALIAS}/{bucket}", arn])
                if rm.returncode != 0:
                    fail(console, f"replace binding {target_id} -> {bucket}: {rm.stderr}")
                    failed += 1
                    continue

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
                fail(console, f"bind {target_id} -> {bucket}: {res.stderr}")
                failed += 1

    changed = targets_set > 0 or bindings_added > 0
    msg = (
        f"{targets_set} target(s), {bindings_added} binding(s), "
        f"restart={'yes' if restart_required else 'no'}"
    )
    if skipped:
        msg += f", {skipped} skipped"
    if failed:
        msg += f", {failed} failed"
    return {"changed": changed, "message": msg, "failed": failed, "items_skipped": skipped}
