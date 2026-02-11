"""
Group Policy Attachment Task

Attaches IAM policies to groups. Runs AFTER the users task because
MinIO groups are implicitly created when users are added to them.
This task ensures the correct policies are attached.

JSON config example:
{
  "groups": [
    {
      "name": "app-services",
      "policies": ["readwrite-documents"]
    }
  ]
}

Notes:
  - Groups are created implicitly by the users task (03_users.py) when
    a user lists the group in their "groups" array.
  - This task only attaches policies. It does NOT create groups without
    members (MinIO does not support empty groups).
  - mc admin policy attach is idempotent.
"""

import subprocess

TASK_NAME = "Groups"
TASK_DESCRIPTION = "Attach policies to groups"
CONFIG_KEY = "groups"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc", "--json"] + args,
        capture_output=True,
        text=True,
    )


def _group_exists(name: str) -> bool:
    """Check if a group already exists (has at least one member)."""
    result = _mc(["admin", "group", "info", MC_ALIAS, name])
    return result.returncode == 0


def run(items: list, console) -> dict:
    if not items:
        return {"skipped": True, "message": "No groups configured"}

    configured = 0

    for group in items:
        name = group["name"]

        if not _group_exists(name):
            console.print(
                f"    [yellow]Warning: Group '{name}' does not exist yet. "
                f"Ensure at least one user is assigned to this group.[/]"
            )
            continue

        console.print(f"    [dim]Group exists: {name}[/]")

        # Attach policies
        for policy_name in group.get("policies", []):
            result = _mc(["admin", "policy", "attach", MC_ALIAS, policy_name, "--group", name])
            if result.returncode == 0:
                console.print(f"    [dim]  Attached policy: {policy_name}[/]")
                configured += 1
            else:
                console.print(f"    [yellow]  Policy attach {policy_name}: {result.stderr.strip()}[/]")

    total = len(items)
    return {
        "changed": configured > 0,
        "message": f"{total} group(s) processed ({configured} policies attached)",
    }
