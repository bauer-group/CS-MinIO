"""
IAM Policy Creation/Update Task

Creates or updates custom IAM policies from JSON policy documents.
Uses mc admin policy create which is idempotent - it creates if missing
and overwrites if already present.

JSON config example:
{
  "policies": [
    {
      "name": "readwrite-documents",
      "statements": [
        {
          "Effect": "Allow",
          "Action": ["s3:GetObject", "s3:PutObject"],
          "Resource": ["arn:aws:s3:::documents/*"]
        }
      ]
    }
  ]
}

A policy that cannot be created or updated counts as a failure (exit 1):
users, groups and service accounts that reference it would otherwise run with
missing or stale permissions.
"""

import json
import os
import tempfile

from ._mc import MC_ALIAS, fail
from ._mc import mc as _mc

TASK_NAME = "Policies"
TASK_DESCRIPTION = "Create or update custom IAM policies"
CONFIG_KEY = "policies"


def _policy_exists(name: str) -> bool:
    """Check if a policy already exists."""
    result = _mc(["admin", "policy", "info", MC_ALIAS, name])
    return result.returncode == 0


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No policies configured"}

    created = 0
    updated = 0
    failed = 0

    for policy in items:
        name = policy["name"]
        existed = _policy_exists(name)

        # Build IAM policy document
        policy_doc = {
            "Version": "2012-10-17",
            "Statement": policy["statements"],
        }

        # Write to temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, prefix=f"policy-{name}-"
        ) as f:
            json.dump(policy_doc, f, indent=2)
            policy_path = f.name

        try:
            # mc admin policy create is idempotent (creates or overwrites)
            result = _mc(["admin", "policy", "create", MC_ALIAS, name, policy_path])

            if result.returncode == 0:
                if existed:
                    updated += 1
                    console.print(f"    [green]Updated policy: {name}[/]")
                else:
                    created += 1
                    console.print(f"    [green]Created policy: {name}[/]")
            else:
                fail(console, f"apply policy {name}: {result.stderr}")
                failed += 1
        finally:
            os.unlink(policy_path)

    total = len(items)
    msg = f"{total} policy/policies processed ({created} created, {updated} updated)"
    if failed:
        msg += f", {failed} failed"
    return {
        "changed": created > 0 or updated > 0,
        "message": msg,
        "failed": failed,
    }
