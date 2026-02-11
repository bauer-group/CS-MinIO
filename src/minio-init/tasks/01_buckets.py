"""
Bucket Creation Task

Creates buckets with optional versioning, quotas, retention policies,
and anonymous access settings.

JSON config example:
{
  "buckets": [
    {
      "name": "documents",
      "region": "us-east-1",
      "versioning": true,
      "quota": { "type": "hard", "size": "10GB" },
      "retention": { "mode": "compliance", "days": 365 },
      "policy": "private"
    }
  ]
}
"""

import json
import subprocess

TASK_NAME = "Buckets"
TASK_DESCRIPTION = "Create and configure S3 buckets"
CONFIG_KEY = "buckets"

MC_ALIAS = "minio"


def _mc(args: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["mc"] + args,
        capture_output=True,
        text=True,
    )


def run(items: list, console) -> dict:
    if not items:
        return {"skipped": True, "message": "No buckets configured"}

    created = 0
    configured = 0

    for bucket in items:
        name = bucket["name"]
        region = bucket.get("region", "")
        target = f"{MC_ALIAS}/{name}"

        # Create bucket
        cmd = ["mb", "--ignore-existing", target]
        if region:
            cmd.extend(["--region", region])
        result = _mc(cmd)

        if result.returncode == 0:
            if "already" not in result.stdout.lower() and "already" not in result.stderr.lower():
                created += 1
                console.print(f"    [green]Created bucket: {name}[/]")
            else:
                console.print(f"    [dim]Bucket exists: {name}[/]")
        else:
            console.print(f"    [red]Failed to create bucket {name}: {result.stderr.strip()}[/]")
            continue

        # Versioning
        if bucket.get("versioning"):
            _mc(["version", "enable", target])
            configured += 1

        # Quota
        quota = bucket.get("quota")
        if quota:
            _mc(["quota", "set", target, "--size", quota["size"]])
            configured += 1

        # Retention
        retention = bucket.get("retention")
        if retention:
            mode = retention.get("mode", "compliance").upper()
            days = str(retention.get("days", 0))
            _mc(["retention", "set", "--default", mode, f"--days={days}", target])
            configured += 1

        # Anonymous policy
        policy = bucket.get("policy", "private")
        if policy == "private":
            _mc(["anonymous", "set", "none", target])
        elif policy == "public":
            _mc(["anonymous", "set", "download", target])
        elif policy == "public-readwrite":
            _mc(["anonymous", "set", "public", target])

    total = len(items)
    return {
        "changed": created > 0 or configured > 0,
        "message": f"{total} bucket(s) processed ({created} created)",
    }
