"""
Bucket Creation Task

Creates buckets with optional configuration:
  - versioning (enable/suspend)
  - object-lock (must be set at creation time, cannot be added later)
  - quota (hard limit)
  - retention (compliance/governance default)
  - lifecycle rules (prefix-based expiration for current/noncurrent versions)
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
      "lifecycle_rules": [
        { "prefix": "daily/", "expire_days": 15 },
        { "prefix": "weekly/", "expire_days": 36 }
      ],
      "policy": "private"
    }
  ]
}

Notes:
  - object_lock enables WORM protection and implies versioning.
    It can ONLY be set at bucket creation time. If the bucket already
    exists without object-lock, a warning is printed.
  - retention requires object_lock to be enabled on the bucket.
  - lifecycle_rules are matched by prefix for idempotency. Existing rules
    with the same prefix are updated if settings differ, or skipped if
    already correct. Rules not in the config are not removed.
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
    result = subprocess.run(
        ["mc", "--json"] + args,
        capture_output=True,
        text=True,
    )
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


def _bucket_exists(name: str) -> bool:
    """Check if a bucket already exists."""
    result = _mc(["stat", f"{MC_ALIAS}/{name}"])
    return result.returncode == 0


def _get_existing_lifecycle_rules(target: str) -> dict:
    """Fetch existing ILM rules and return them keyed by prefix.

    Returns:
        Dict mapping prefix -> {"id", "expire_days", "noncurrent_expire_days",
        "expire_delete_marker"}.
    """
    result = _mc(["ilm", "rule", "ls", target])
    rules = {}
    if result.returncode != 0:
        return rules

    for line in result.stdout.strip().splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not data.get("id"):
            continue
        prefix = data.get("prefix", "")
        expiration = data.get("expiration", {})
        noncurrent = data.get("noncurrentExpiration", {})
        rules[prefix] = {
            "id": data["id"],
            "expire_days": expiration.get("days", 0),
            "expire_delete_marker": expiration.get("deleteMarker", False),
            "noncurrent_expire_days": noncurrent.get("days", 0),
        }
    return rules


def _build_ilm_add_cmd(target: str, rule: dict) -> list:
    """Build mc ilm rule add command from a rule config dict."""
    cmd = ["ilm", "rule", "add"]

    prefix = rule.get("prefix", "")
    if prefix:
        cmd.extend(["--prefix", prefix])

    if rule.get("expire_days"):
        cmd.extend(["--expire-days", str(rule["expire_days"])])

    if rule.get("noncurrent_expire_days"):
        cmd.extend(["--noncurrent-expire-days", str(rule["noncurrent_expire_days"])])

    if rule.get("expire_delete_marker"):
        cmd.append("--expire-delete-marker")

    cmd.append(target)
    return cmd


def _rule_matches(existing: dict, desired: dict) -> bool:
    """Check if an existing rule's settings match the desired config."""
    if existing["expire_days"] != desired.get("expire_days", 0):
        return False
    if existing["noncurrent_expire_days"] != desired.get("noncurrent_expire_days", 0):
        return False
    if existing["expire_delete_marker"] != desired.get("expire_delete_marker", False):
        return False
    return True


def run(items: list, console, **kwargs) -> dict:
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

            if retention.get("years"):
                validity = f"{retention['years']}y"
            else:
                validity = f"{retention.get('days', 0)}d"

            result = _mc(["retention", "set", "--default", mode, validity, target])
            if result.returncode == 0:
                console.print(f"    [dim]  Retention: {mode} {validity}[/]")
                configured += 1
            else:
                console.print(f"    [yellow]  Retention set failed: {result.stderr.strip()}[/]")
                if not want_object_lock:
                    console.print(f"    [yellow]  Hint: retention requires object_lock to be enabled[/]")

        # --- Lifecycle Rules ---
        lifecycle_rules = bucket.get("lifecycle_rules", [])
        if lifecycle_rules:
            existing_rules = _get_existing_lifecycle_rules(target)
            rules_added = 0
            rules_updated = 0
            rules_unchanged = 0

            for rule in lifecycle_rules:
                prefix = rule.get("prefix", "")
                existing = existing_rules.get(prefix)

                if existing and _rule_matches(existing, rule):
                    rules_unchanged += 1
                    continue

                if existing:
                    # Settings differ -> remove old rule first
                    _mc(["ilm", "rule", "rm", target, "--id", existing["id"]])

                cmd = _build_ilm_add_cmd(target, rule)
                add_result = _mc(cmd)
                if add_result.returncode == 0:
                    if existing:
                        rules_updated += 1
                        console.print(f"    [green]  Lifecycle rule updated: prefix='{prefix}'[/]")
                    else:
                        rules_added += 1
                        console.print(f"    [green]  Lifecycle rule added: prefix='{prefix}'[/]")
                    configured += 1
                else:
                    console.print(
                        f"    [yellow]  Lifecycle rule failed for prefix='{prefix}': "
                        f"{add_result.stderr.strip()}[/]"
                    )

            if rules_added or rules_updated:
                summary = []
                if rules_added:
                    summary.append(f"{rules_added} added")
                if rules_updated:
                    summary.append(f"{rules_updated} updated")
                if rules_unchanged:
                    summary.append(f"{rules_unchanged} unchanged")
                console.print(f"    [dim]  Lifecycle: {', '.join(summary)}[/]")
            elif rules_unchanged:
                console.print(f"    [dim]  Lifecycle: {rules_unchanged} rule(s) already configured[/]")

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
