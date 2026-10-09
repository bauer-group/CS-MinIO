"""
Bucket Creation Task

Creates buckets with optional configuration:
  - versioning (enable/suspend)
  - object-lock (must be set at creation time, cannot be added later)
  - quota (hard limit)
  - retention (compliance/governance default)
  - lifecycle rules (prefix-based expiration for current/noncurrent versions)
  - anonymous access policy (private/public/public-readwrite)
  - CORS rules (engine-dependent; declared here, applied by the storage engine)

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
      "policy": "private",
      "cors": [
        {
          "allowed_origins": ["https://app.example.com"],
          "allowed_methods": ["GET", "PUT", "POST", "HEAD"],
          "allowed_headers": ["*"],
          "expose_headers": ["ETag"],
          "max_age_seconds": 3600
        }
      ]
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
    already correct; extra copies of a configured rule (earlier versions
    added one on every run) are removed. Rules whose prefix is not in the
    config are not touched.
  - cors is an S3-compatible per-bucket CORS ruleset. It is validated here but
    NOT applied to MinIO: open-source MinIO has no per-bucket CORS API and
    enforces CORS globally via the MINIO_API_CORS_ALLOW_ORIGIN server setting
    (default "*"). The field exists so applications can declare CORS today; a
    storage engine with per-bucket CORS (e.g. SeaweedFS via PutBucketCors) can
    apply the same config later without any application change.
  - All operations are idempotent. Existing settings are re-applied
    (no-op if unchanged) rather than skipped.
  - Every requested setting that cannot be applied (bucket creation,
    versioning, quota, retention, lifecycle rule, anonymous policy) counts as a
    failure and makes the init container exit 1. An unknown "policy" value
    (the anonymous policy is then left as it is) and invalid CORS rules are
    warnings.
"""

from ._mc import MC_ALIAS, fail, iter_json, warn
from ._mc import mc as _mc

TASK_NAME = "Buckets"
TASK_DESCRIPTION = "Create and configure S3 buckets"
CONFIG_KEY = "buckets"

# Config value -> `mc anonymous set` permission. "public" is read-only on purpose.
ANONYMOUS_POLICIES = {
    "private": "none",
    "public": "download",
    "public-readwrite": "public",
}


def _bucket_exists(name: str) -> bool:
    """Check if a bucket already exists."""
    result = _mc(["stat", f"{MC_ALIAS}/{name}"])
    return result.returncode == 0


def _get_existing_lifecycle_rules(target: str) -> dict:
    """Fetch existing ILM rules and group them by prefix.

    `mc ilm rule ls --json` prints one document with minio-go's lifecycle JSON:
    {"status": "success", "config": {"Rules": [{"ID": "...", "Filter": {"Prefix":
    "daily/"}, "Expiration": {"Days": 15}, "NoncurrentVersionExpiration":
    {"NoncurrentDays": 30}, ...}]}}. Empty values are omitted. A bucket without
    rules makes the command fail ("lifecycle configuration not set"), which is
    read as "no rules".

    Returns:
        Dict mapping prefix -> list of {"id", "expire_days",
        "noncurrent_expire_days", "expire_delete_marker"}.
    """
    result = _mc(["ilm", "rule", "ls", target])
    rules = {}
    if result.returncode != 0:
        return rules

    for doc in iter_json(result.stdout):
        if not isinstance(doc, dict):
            continue
        for rule in (doc.get("config") or {}).get("Rules") or []:
            if not isinstance(rule, dict) or not rule.get("ID"):
                continue
            rule_filter = rule.get("Filter") or {}
            prefix = (
                rule_filter.get("Prefix")
                or (rule_filter.get("And") or {}).get("Prefix")
                or rule.get("Prefix")
                or ""
            )
            expiration = rule.get("Expiration") or {}
            noncurrent = rule.get("NoncurrentVersionExpiration") or {}
            rules.setdefault(prefix, []).append({
                "id": rule["ID"],
                "expire_days": expiration.get("Days") or 0,
                "expire_delete_marker": bool(expiration.get("ExpiredObjectDeleteMarker")),
                "noncurrent_expire_days": noncurrent.get("NoncurrentDays") or 0,
            })
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


CORS_ALLOWED_METHODS = {"GET", "PUT", "POST", "DELETE", "HEAD"}


def _validate_cors_rules(rules) -> list:
    """Validate an S3-compatible per-bucket CORS ruleset.

    Returns a list of human-readable error strings (empty = valid). Validation is
    structural only — it does not apply anything to the storage engine. Kept as a
    standalone helper so an engine that supports per-bucket CORS can reuse it.
    """
    errors = []

    if not isinstance(rules, list):
        return [f"cors must be a list of rules, got {type(rules).__name__}"]

    for i, rule in enumerate(rules):
        where = f"cors[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{where} must be an object")
            continue

        origins = rule.get("allowed_origins")
        if not isinstance(origins, list) or not origins:
            errors.append(f"{where}.allowed_origins must be a non-empty list")

        methods = rule.get("allowed_methods")
        if not isinstance(methods, list) or not methods:
            errors.append(f"{where}.allowed_methods must be a non-empty list")
        else:
            invalid = [m for m in methods if m not in CORS_ALLOWED_METHODS]
            if invalid:
                errors.append(
                    f"{where}.allowed_methods has unsupported method(s): {', '.join(map(str, invalid))} "
                    f"(allowed: {', '.join(sorted(CORS_ALLOWED_METHODS))})"
                )

        for opt_list in ("allowed_headers", "expose_headers"):
            if opt_list in rule and not isinstance(rule[opt_list], list):
                errors.append(f"{where}.{opt_list} must be a list")

        max_age = rule.get("max_age_seconds")
        if max_age is not None and (not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0):
            errors.append(f"{where}.max_age_seconds must be a non-negative integer")

    return errors




def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No buckets configured"}

    created = 0
    configured = 0
    warnings = 0
    failed = 0

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
                fail(console, f"create bucket {name}: {result.stderr}")
                failed += 1
                continue
        else:
            console.print(f"    [dim]Bucket exists: {name}[/]")
            # Warn if object-lock was requested but bucket already exists without it
            if want_object_lock:
                warn(
                    console,
                    "object_lock requested but bucket already exists. "
                    "Object-lock can only be enabled at creation time.",
                )
                warnings += 1

        # --- Versioning ---
        versioning = bucket.get("versioning", want_object_lock)
        if versioning:
            result = _mc(["version", "enable", target])
            if result.returncode == 0:
                configured += 1
            else:
                fail(console, f"enable versioning on {name}: {result.stderr}")
                failed += 1

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
                fail(console, f"set quota on {name}: {result.stderr}")
                failed += 1

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
                hint = "" if want_object_lock else " (retention requires object_lock on the bucket)"
                fail(console, f"set retention on {name}: {result.stderr}{hint}")
                failed += 1

        # --- Lifecycle Rules ---
        lifecycle_rules = bucket.get("lifecycle_rules", [])
        if lifecycle_rules:
            existing_rules = _get_existing_lifecycle_rules(target)
            rules_added = 0
            rules_removed = 0
            rules_unchanged = 0

            desired_by_prefix = {}
            for rule in lifecycle_rules:
                desired_by_prefix.setdefault(rule.get("prefix", ""), []).append(rule)

            for prefix, desired in desired_by_prefix.items():
                # Pair every configured rule with one identical existing rule; whatever
                # is left over under this prefix is stale (other settings) or a copy.
                remaining = list(existing_rules.get(prefix, []))
                to_add = []
                for rule in desired:
                    match = next((r for r in remaining if _rule_matches(r, rule)), None)
                    if match:
                        remaining.remove(match)
                        rules_unchanged += 1
                    else:
                        to_add.append(rule)

                prefix_failed = False
                for stale in remaining:
                    rm_result = _mc(["ilm", "rule", "rm", "--id", stale["id"], target])
                    if rm_result.returncode == 0:
                        rules_removed += 1
                    else:
                        fail(console, f"remove lifecycle rule {stale['id']} prefix='{prefix}' on {name}: "
                                      f"{rm_result.stderr}")
                        failed += 1
                        prefix_failed = True
                if prefix_failed:
                    continue

                for rule in to_add:
                    add_result = _mc(_build_ilm_add_cmd(target, rule))
                    if add_result.returncode == 0:
                        rules_added += 1
                        configured += 1
                        console.print(f"    [green]  Lifecycle rule added: prefix='{prefix}'[/]")
                    else:
                        fail(console, f"add lifecycle rule prefix='{prefix}' on {name}: {add_result.stderr}")
                        failed += 1

            if rules_added or rules_removed:
                summary = []
                if rules_added:
                    summary.append(f"{rules_added} added")
                if rules_removed:
                    summary.append(f"{rules_removed} outdated or duplicate removed")
                if rules_unchanged:
                    summary.append(f"{rules_unchanged} unchanged")
                console.print(f"    [dim]  Lifecycle: {', '.join(summary)}[/]")
                configured += 1
            elif rules_unchanged:
                console.print(f"    [dim]  Lifecycle: {rules_unchanged} rule(s) already configured[/]")

        # --- Anonymous access policy ---
        policy = bucket.get("policy", "private")
        permission = ANONYMOUS_POLICIES.get(policy)
        if permission is None:
            # Unknown values were silently ignored before; stay non-destructive and
            # leave the bucket's anonymous policy untouched, but say so.
            warn(
                console,
                f"unknown policy '{policy}' on {name} - anonymous access left unchanged "
                f"(use one of: {', '.join(ANONYMOUS_POLICIES)})",
            )
            warnings += 1
        else:
            result = _mc(["anonymous", "set", permission, target])
            if result.returncode != 0:
                fail(console, f"set anonymous policy '{policy}' on {name}: {result.stderr}")
                failed += 1

        # --- CORS (engine-dependent; declared but not applied on MinIO) ---
        cors = bucket.get("cors")
        if cors:
            cors_errors = _validate_cors_rules(cors)
            for err in cors_errors:
                warn(console, f"CORS config invalid: {err}")
                warnings += 1
            if not cors_errors:
                console.print(
                    f"    [dim]  CORS: {len(cors)} rule(s) declared - not applied on MinIO "
                    f"(global CORS via MINIO_API_CORS_ALLOW_ORIGIN); applied by engines with "
                    f"per-bucket CORS[/]"
                )

    total = len(items)
    msg = f"{total} bucket(s) processed ({created} created)"
    if warnings:
        msg += f", {warnings} warning(s)"
    if failed:
        msg += f", {failed} failed"
    return {
        "changed": created > 0 or configured > 0,
        "message": msg,
        "failed": failed,
    }
