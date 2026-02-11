"""
Service Account Creation Task

Creates service accounts (access key pairs) bound to parent users.
Service accounts inherit the parent user's policies unless overridden.

JSON config example:
{
  "service_accounts": [
    {
      "user": "app-service",
      "access_key": "my-sa-access-key",
      "secret_key": "${MY_SA_SECRET_KEY}",
      "name": "My Service Account",
      "description": "Used by application X"
    }
  ]
}
"""

import subprocess

TASK_NAME = "Service Accounts"
TASK_DESCRIPTION = "Create service accounts with explicit credentials"
CONFIG_KEY = "service_accounts"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc"] + args,
        capture_output=True,
        text=True,
    )


def run(items: list, console) -> dict:
    if not items:
        return {"skipped": True, "message": "No service accounts configured"}

    created = 0

    for sa in items:
        parent_user = sa["user"]
        access_key = sa["access_key"]
        secret_key = sa["secret_key"]
        sa_name = sa.get("name", "")
        sa_description = sa.get("description", "")

        # Build command
        cmd = [
            "admin", "user", "svcacct", "add",
            MC_ALIAS, parent_user,
            "--access-key", access_key,
            "--secret-key", secret_key,
        ]
        if sa_name:
            cmd.extend(["--name", sa_name])
        if sa_description:
            cmd.extend(["--description", sa_description])

        result = _mc(cmd)

        if result.returncode == 0:
            created += 1
            console.print(f"    [green]Created service account: {access_key} (parent: {parent_user})[/]")
        elif "already exists" in result.stderr.lower():
            console.print(f"    [dim]Service account exists: {access_key}[/]")
        else:
            console.print(f"    [red]Failed to create SA {access_key}: {result.stderr.strip()}[/]")

    total = len(items)
    return {
        "changed": created > 0,
        "message": f"{total} service account(s) processed ({created} created)",
    }
