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

MinIO only knows a group once it has a member. A group whose users were all
skipped (optional users with an empty secret) or that has no users at all
therefore does not exist yet: it is reported as skipped, not failed - there is
nobody the policies could apply to. Any other attach error (e.g. a policy that
does not exist) is a failure and the init container exits 1.
"""

from ._mc import MC_ALIAS, fail, skip
from ._mc import mc as _mc

TASK_NAME = "Groups"
TASK_DESCRIPTION = "Attach policies to groups"
CONFIG_KEY = "groups"


def _is_missing_group(error: str) -> bool:
    """mc's message for a group MinIO does not know (XMinioAdminNoSuchGroup)."""
    return "specified group does not exist" in error.lower()


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No groups configured"}

    created = 0
    configured = 0
    skipped = 0
    failed = 0

    for group in items:
        name = group["name"]
        policies = group.get("policies", [])

        if not policies:
            skip(console, f"group '{name}' has no policies (at least one required)")
            skipped += 1
            continue

        # Attach policies (mc reports an already attached policy as success)
        status = "ok"
        for policy_name in policies:
            result = _mc(["admin", "policy", "attach", MC_ALIAS, policy_name, "--group", name])
            if result.returncode == 0:
                console.print(f"    [dim]  Attached policy: {policy_name} → {name}[/]")
                configured += 1
            elif _is_missing_group(result.stderr):
                skip(
                    console,
                    f"group '{name}' has no members (its users were skipped or none are "
                    f"configured) - policies not attached",
                )
                skipped += 1
                status = "skipped"
                break
            else:
                fail(console, f"attach policy {policy_name} to group {name}: {result.stderr}")
                failed += 1
                status = "failed"

        if status == "ok":
            created += 1
            console.print(f"    [green]Created/updated group: {name}[/]")

    total = len(items)
    msg = f"{total} group(s) processed ({created} created/updated, {configured} policies attached"
    if skipped:
        msg += f", {skipped} skipped"
    if failed:
        msg += f", {failed} failed"
    return {
        "changed": created > 0,
        "message": msg + ")",
        "failed": failed,
        "items_skipped": skipped,
    }
