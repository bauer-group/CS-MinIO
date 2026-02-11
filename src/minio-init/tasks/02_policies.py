"""
IAM Policy Creation Task

Creates custom IAM policies from JSON policy documents.

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
"""

import json
import subprocess
import tempfile

TASK_NAME = "Policies"
TASK_DESCRIPTION = "Create custom IAM policies"
CONFIG_KEY = "policies"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc"] + args,
        capture_output=True,
        text=True,
    )


def run(items: list, console) -> dict:
    if not items:
        return {"skipped": True, "message": "No policies configured"}

    created = 0

    for policy in items:
        name = policy["name"]

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

        # Create policy (idempotent: remove + recreate to update)
        result = _mc(["admin", "policy", "create", MC_ALIAS, name, policy_path])

        if result.returncode == 0:
            created += 1
            console.print(f"    [green]Created policy: {name}[/]")
        elif "already exists" in result.stderr.lower():
            # Remove and recreate to ensure latest version
            _mc(["admin", "policy", "remove", MC_ALIAS, name])
            result = _mc(["admin", "policy", "create", MC_ALIAS, name, policy_path])
            if result.returncode == 0:
                created += 1
                console.print(f"    [green]Updated policy: {name}[/]")
            else:
                console.print(f"    [red]Failed to update policy {name}: {result.stderr.strip()}[/]")
        else:
            console.print(f"    [red]Failed to create policy {name}: {result.stderr.strip()}[/]")

    total = len(items)
    return {
        "changed": created > 0,
        "message": f"{total} policy/policies processed ({created} created/updated)",
    }
