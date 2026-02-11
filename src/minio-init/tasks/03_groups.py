"""
Group Creation and Policy Attachment Task

Creates groups by attaching IAM policies. Runs BEFORE the users task
in the logical order: bucket → policy → group → user.

A group in MinIO is implicitly created when a policy is attached to it
via mc admin policy attach --group. Each group must have at least one
policy assigned.

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
        ["mc", "--json"] + args,
        capture_output=True,
        text=True,
    )


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No groups configured"}

    created = 0
    configured = 0

    for group in items:
        name = group["name"]
        policies = group.get("policies", [])

        if not policies:
            console.print(f"    [yellow]Warning: Group '{name}' has no policies - skipped (at least one required)[/]")
            continue

        # Attach policies (implicitly creates the group)
        for policy_name in policies:
            result = _mc(["admin", "policy", "attach", MC_ALIAS, policy_name, "--group", name])
            if result.returncode == 0:
                console.print(f"    [dim]  Attached policy: {policy_name} → {name}[/]")
                configured += 1
            else:
                console.print(f"    [yellow]  Policy attach {policy_name} → {name}: {result.stderr.strip()}[/]")

        created += 1
        console.print(f"    [green]Created/updated group: {name}[/]")

    total = len(items)
    return {
        "changed": created > 0,
        "message": f"{total} group(s) processed ({created} created/updated, {configured} policies attached)",
    }
