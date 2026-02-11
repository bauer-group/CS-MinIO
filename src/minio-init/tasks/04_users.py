"""
User Creation Task

Creates MinIO users with group membership and direct policy attachments.
Runs AFTER the groups task in the order: bucket → policy → group → user.

Groups already exist at this point (created by policy attachment in
03_groups.py). This task adds users as members and attaches any direct
user-level policies.

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

import subprocess

TASK_NAME = "Users"
TASK_DESCRIPTION = "Create users and assign group membership"
CONFIG_KEY = "users"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc", "--json"] + args,
        capture_output=True,
        text=True,
    )


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No users configured"}

    created = 0

    for user in items:
        access_key = user["access_key"]
        secret_key = user["secret_key"]

        # Create user (idempotent: updates password if user exists)
        result = _mc(["admin", "user", "add", MC_ALIAS, access_key, secret_key])

        if result.returncode == 0:
            created += 1
            console.print(f"    [green]Created/updated user: {access_key}[/]")
        else:
            console.print(f"    [red]Failed to create user {access_key}: {result.stderr.strip()}[/]")
            continue

        # Add to groups (groups already exist from 03_groups task)
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
