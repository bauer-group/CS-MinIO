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

Optional users: a user whose access_key or secret_key resolves to an empty
string (e.g. "${BACKUP_PASSWORD}" with the variable left empty) is skipped, not
failed, and so are the service accounts that belong to it. Every other error
(rejected secret, unknown group or policy) is a failure and the init container
exits 1.

The groups of skipped and of failed users are recorded in the run context
(skipped_user_groups, failed_user_groups). The groups task needs them to tell
a group whose users were all skipped (optional) from one that no configured
user is in, e.g. a typo in a user's "groups" (fatal).
"""

import os

from ._mc import MC_ALIAS, fail, skip
from ._mc import mc as _mc

TASK_NAME = "Users"
TASK_DESCRIPTION = "Create users and assign group membership"
CONFIG_KEY = "users"


def run(items: list, console, **kwargs) -> dict:
    if not items:
        return {"skipped": True, "message": "No users configured"}

    context = kwargs.get("context", {})
    # Shared with the service-accounts task, which skips accounts of skipped users.
    skipped_users = context.setdefault("skipped_users", set())
    # Shared with the groups task (see the module docstring).
    skipped_user_groups = context.setdefault("skipped_user_groups", set())
    failed_user_groups = context.setdefault("failed_user_groups", set())

    created = 0
    skipped = 0
    failed = 0
    root_user = os.environ.get("MINIO_ROOT_USER", "minioadmin")

    for user in items:
        access_key = user["access_key"]
        secret_key = user["secret_key"]
        user_groups = user.get("groups", [])

        if not access_key or not secret_key:
            empty = "access_key" if not access_key else "secret_key"
            skip(console, f"user '{access_key}': {empty} is empty - optional user not created")
            skipped_users.add(access_key)
            skipped_user_groups.update(user_groups)
            skipped += 1
            continue

        # Skip root user - cannot be managed as IAM user
        if access_key == root_user:
            skip(console, f"'{access_key}': this is the root user (MINIO_ROOT_USER), not an IAM user")
            skipped_user_groups.update(user_groups)
            skipped += 1
            continue

        # Create user (idempotent: updates password if user exists)
        result = _mc(["admin", "user", "add", MC_ALIAS, access_key, secret_key])

        if result.returncode == 0:
            created += 1
            console.print(f"    [green]Created/updated user: {access_key}[/]")
        else:
            fail(console, f"create user {access_key}: {result.stderr}")
            failed_user_groups.update(user_groups)
            failed += 1
            continue

        # Add to groups (groups created implicitly, policies attached by 04_groups task)
        for group_name in user_groups:
            result = _mc(["admin", "group", "add", MC_ALIAS, group_name, access_key])
            if result.returncode == 0:
                console.print(f"    [dim]  Added to group: {group_name}[/]")
            else:
                fail(console, f"add user {access_key} to group {group_name}: {result.stderr}")
                failed_user_groups.add(group_name)
                failed += 1

        # Attach direct policies (mc reports an already attached policy as success)
        for policy_name in user.get("policies", []):
            result = _mc(["admin", "policy", "attach", MC_ALIAS, policy_name, "--user", access_key])
            if result.returncode == 0:
                console.print(f"    [dim]  Attached policy: {policy_name}[/]")
            else:
                fail(console, f"attach policy {policy_name} to user {access_key}: {result.stderr}")
                failed += 1

    total = len(items)
    msg = f"{total} user(s) processed ({created} created/updated"
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
