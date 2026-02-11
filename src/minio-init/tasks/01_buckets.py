"""
Bucket Creation Task

Creates buckets with optional configuration:
  - versioning (enable/suspend)
  - object-lock (must be set at creation time, cannot be added later)
  - quota (hard limit)
  - retention (compliance/governance default)
  - anonymous access policy (private/public/public-readwrite)

JSON config example:
{
  "buckets": [
    {
      "name": "documents",
      "region": "eu-central-1",
      "versioning": true,
      "object_lock": true,
      "quota": { "type": "hard", "size": "10GB" },
      "retention": { "mode": "compliance", "days": 365 },
      "policy": "private"
    }
  ]
}

Notes:
  - object_lock enables WORM protection and implies versioning.
    It can ONLY be set at bucket creation time. If the bucket already
    exists without object-lock, a warning is printed.
  - retention requires object_lock to be enabled on the bucket.
  - All operations are idempotent. Existing settings are re-applied
    (no-op if unchanged) rather than skipped.
"""

import json
import subprocess

TASK_NAME = "Buckets"
TASK_DESCRIPTION = "Create and configure S3 buckets"
CONFIG_KEY = "buckets"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc", "--json"] + args,
        capture_output=True,
        text=True,
    )


def _mc_json(args: list) -> dict | None:
    """Run mc command and parse JSON output."""
    result = _mc(args)
    if result.returncode == 0 and result.stdout.strip():
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            pass
    return None


def _bucket_exists(name: str) -> bool:
    """Check if a bucket already exists."""
    result = _mc(["stat", f"{MC_ALIAS}/{name}"])
    return result.returncode == 0


def run(items: list, console) -> dict:
    if not items:
        return {"skipped": True, "message": "No buckets configured"}

    created = 0
    configured = 0
    warnings = 0

    for bucket in items:
        name = bucket["name"]
        region = bucket.get("region", "")
        target = f"{MC_ALIAS}/{name}"
        want_object_lock = bucket.get("object_lock", False)
        exists = _bucket_exists(name)

        # --- Create bucket ---
        if not exists:
            cmd = ["mb"]
            if want_object_lock:
                cmd.append("--with-lock")
            if region:
                cmd.extend(["--region", region])
            cmd.append(target)

            result = _mc(cmd)
            if result.returncode == 0:
                created += 1
                lock_note = " (with object-lock)" if want_object_lock else ""
                console.print(f"    [green]Created bucket: {name}{lock_note}[/]")
            else:
                console.print(f"    [red]Failed to create bucket {name}: {result.stderr.strip()}[/]")
                continue
        else:
            console.print(f"    [dim]Bucket exists: {name}[/]")
            # Warn if object-lock was requested but bucket already exists without it
            if want_object_lock:
                info = _mc_json(["stat", target])
                # mc stat doesn't reliably report object-lock, so we warn generically
                console.print(
                    f"    [yellow]Warning: object_lock requested but bucket already exists. "
                    f"Object-lock can only be enabled at creation time.[/]"
                )
                warnings += 1

        # --- Versioning ---
        versioning = bucket.get("versioning", want_object_lock)
        if versioning:
            result = _mc(["version", "enable", target])
            if result.returncode == 0:
                configured += 1
            else:
                console.print(f"    [yellow]Warning: versioning enable failed: {result.stderr.strip()}[/]")

        # --- Quota ---
        quota = bucket.get("quota")
        if quota:
            quota_type = quota.get("type", "hard")
            quota_size = quota["size"]
            result = _mc(["quota", "set", target, "--size", quota_size])
            if result.returncode == 0:
                console.print(f"    [dim]  Quota: {quota_type} {quota_size}[/]")
                configured += 1
            else:
                console.print(f"    [yellow]  Quota set failed: {result.stderr.strip()}[/]")

        # --- Retention (requires object-lock) ---
        retention = bucket.get("retention")
        if retention:
            mode = retention.get("mode", "compliance").upper()
            validity = str(retention.get("days", 0))
            validity_unit = "DAYS"

            if retention.get("years"):
                validity = str(retention["years"])
                validity_unit = "YEARS"

            result = _mc(["retention", "set", "--default", mode, f"--{validity_unit.lower()}", validity, target])
            if result.returncode == 0:
                console.print(f"    [dim]  Retention: {mode} {validity} {validity_unit.lower()}[/]")
                configured += 1
            else:
                console.print(f"    [yellow]  Retention set failed: {result.stderr.strip()}[/]")
                if not want_object_lock:
                    console.print(f"    [yellow]  Hint: retention requires object_lock to be enabled[/]")

        # --- Anonymous access policy ---
        policy = bucket.get("policy", "private")
        if policy == "private":
            _mc(["anonymous", "set", "none", target])
        elif policy == "public":
            _mc(["anonymous", "set", "download", target])
        elif policy == "public-readwrite":
            _mc(["anonymous", "set", "public", target])

    total = len(items)
    msg = f"{total} bucket(s) processed ({created} created)"
    if warnings:
        msg += f", {warnings} warning(s)"
    return {
        "changed": created > 0 or configured > 0,
        "message": msg,
    }
