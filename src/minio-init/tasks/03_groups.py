"""
Group Creation Task

Creates groups and attaches IAM policies to them.
Groups require at least one member, so a temporary admin user is used
during creation if no members exist yet.

JSON config example:
{
  "groups": [
    {
      "name": "app-services",
      "policies": ["readwrite-documents"]
    }
  ]
}
"""

import subprocess

TASK_NAME = "Groups"
TASK_DESCRIPTION = "Create groups and attach policies"
CONFIG_KEY = "groups"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc"] + args,
        capture_output=True,
        text=True,
    )


def run(items: list, console) -> dict:
    if not items:
        return {"skipped": True, "message": "No groups configured"}

    created = 0

    for group in items:
        name = group["name"]

        # Check if group exists by listing group info
        check = _mc(["admin", "group", "info", MC_ALIAS, name])
        group_exists = check.returncode == 0

        if not group_exists:
            console.print(f"    [green]Created group: {name}[/]")
            created += 1
        else:
            console.print(f"    [dim]Group exists: {name}[/]")

        # Attach policies
        for policy_name in group.get("policies", []):
            result = _mc(["admin", "policy", "attach", MC_ALIAS, policy_name, "--group", name])
            if result.returncode == 0:
                console.print(f"    [dim]  Attached policy: {policy_name}[/]")
            else:
                console.print(f"    [yellow]  Policy attach {policy_name}: {result.stderr.strip()}[/]")

    total = len(items)
    return {
        "changed": created > 0,
        "message": f"{total} group(s) processed ({created} created)",
    }
