"""
User Creation Task

Creates MinIO users with group membership and direct policy attachments.
Runs BEFORE the groups task in the order: bucket → policy → user → group.

Groups are implicitly created by mc admin group add when adding users.
The groups task (04) then attaches policies to these groups. This ordering
ensures policy attachments are not overwritten by group membership updates.

JSON config example:
{
  "users": [
    {
      "access_key": "app-service",
      "secret_key": "${APP_SECRET}",
      "groups": ["app-services"],
      "policies": ["readwrite-documents"]
    }
  ]
}
"""

import json
import os
import subprocess

TASK_NAME = "Users"
TASK_DESCRIPTION = "Create users and assign group membership"
CONFIG_KEY = "users"

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


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No users configured"}

    created = 0
    root_user = os.environ.get("MINIO_ROOT_USER", "minioadmin")

    for user in items:
        access_key = user["access_key"]
        secret_key = user["secret_key"]

        # Skip root user - cannot be managed as IAM user
        if access_key == root_user:
            console.print(
                f"    [yellow]Skipped '{access_key}': this is the root user "
                f"(MINIO_ROOT_USER), not an IAM user[/]"
            )
            continue

        # Create user (idempotent: updates password if user exists)
        result = _mc(["admin", "user", "add", MC_ALIAS, access_key, secret_key])

        if result.returncode == 0:
            created += 1
            console.print(f"    [green]Created/updated user: {access_key}[/]")
        else:
            console.print(f"    [red]Failed to create user {access_key}: {result.stderr.strip()}[/]")
            continue

        # Add to groups (groups created implicitly, policies attached by 04_groups task)
        for group_name in user.get("groups", []):
            result = _mc(["admin", "group", "add", MC_ALIAS, group_name, access_key])
            if result.returncode == 0:
                console.print(f"    [dim]  Added to group: {group_name}[/]")
            else:
                console.print(f"    [yellow]  Group add {group_name}: {result.stderr.strip()}[/]")

        # Attach direct policies
        for policy_name in user.get("policies", []):
            result = _mc(["admin", "policy", "attach", MC_ALIAS, policy_name, "--user", access_key])
            if result.returncode == 0:
                console.print(f"    [dim]  Attached policy: {policy_name}[/]")
            else:
                console.print(f"    [yellow]  Policy attach {policy_name}: {result.stderr.strip()}[/]")

    total = len(items)
    return {
        "changed": created > 0,
        "message": f"{total} user(s) processed ({created} created/updated)",
    }
