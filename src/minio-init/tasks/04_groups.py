"""
Group Policy Attachment Task

Attaches IAM policies to groups. Runs AFTER the users task in the order:
bucket → policy → user → group.

Groups are implicitly created when users are added (mc admin group add
in 03_users.py). This task then attaches policies to the existing groups
via mc admin policy attach --group. Running AFTER user creation ensures
that group membership updates cannot overwrite policy attachments.

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

import json
import subprocess

TASK_NAME = "Groups"
TASK_DESCRIPTION = "Attach policies to groups"
CONFIG_KEY = "groups"

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
        return {"skipped": True, "message": "No groups configured"}

    created = 0
    configured = 0

    for group in items:
        name = group["name"]
        policies = group.get("policies", [])

        if not policies:
            console.print(f"    [yellow]Warning: Group '{name}' has no policies - skipped (at least one required)[/]")
            continue

        # Attach policies (implicitly creates the group if it doesn't exist)
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
