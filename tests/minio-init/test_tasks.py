"""Failure vs. skip semantics of every init task (mc is faked, see conftest)."""

import json
import socket
from pathlib import Path

import pytest
from conftest import load_task, output, random_secret

buckets = load_task("01_buckets")
policies = load_task("02_policies")
users = load_task("03_users")
groups = load_task("04_groups")
service_accounts = load_task("05_service_accounts")
notifications = load_task("06_notifications")

NO_SUCH_GROUP = "The specified group does not exist."
NO_SUCH_POLICY = "The canned policy does not exist."


# --- buckets ----------------------------------------------------------------

def _missing_bucket(mc, name):
    mc.fail("stat", f"minio/{name}", message="Unable to stat", cause="Bucket does not exist.")


def test_bucket_creation_failure_is_fatal_and_stops_that_bucket(mc, console):
    _missing_bucket(mc, "docs")
    mc.fail("mb", message="Unable to make bucket", cause="Region does not match.")

    result = buckets.run([{"name": "docs", "versioning": True}], console)

    assert result["failed"] == 1
    assert not mc.called("version")
    assert "Failed: create bucket docs" in output(console)


@pytest.mark.parametrize("prefix,setting", [
    (("version", "enable"), {"versioning": True}),
    (("quota", "set"), {"quota": {"type": "hard", "size": "1GB"}}),
    (("retention", "set"), {"retention": {"mode": "compliance", "days": 1}}),
    (("ilm", "rule", "add"), {"lifecycle_rules": [{"prefix": "daily/", "expire_days": 1}]}),
    (("anonymous", "set"), {"policy": "private"}),
])
def test_each_bucket_setting_that_cannot_be_applied_is_fatal(mc, console, prefix, setting):
    mc.fail(*prefix)

    result = buckets.run([{"name": "docs", **setting}], console)

    assert result["failed"] == 1


def test_retention_failure_without_object_lock_carries_a_hint(mc, console):
    mc.fail("retention", "set", message="Unable to set retention", cause="Bucket is missing ObjectLockConfiguration")

    buckets.run([{"name": "docs", "retention": {"mode": "compliance", "days": 1}}], console)

    assert "retention requires object_lock" in output(console)


@pytest.mark.parametrize("policy,permission", [
    ("private", "none"), ("public", "download"), ("public-readwrite", "public"),
])
def test_bucket_policy_maps_to_the_anonymous_permission(mc, console, policy, permission):
    result = buckets.run([{"name": "docs", "policy": policy}], console)

    assert result["failed"] == 0
    assert mc.called("anonymous", "set") == [["anonymous", "set", permission, "minio/docs"]]


@pytest.mark.parametrize("policy", ["none", "download"])
def test_unknown_bucket_policy_warns_and_leaves_access_unchanged(mc, console, policy):
    result = buckets.run([{"name": "docs", "policy": policy}], console)

    assert result["failed"] == 0
    assert not mc.called("anonymous")
    assert f"unknown policy '{policy}'" in output(console)


def test_object_lock_on_existing_bucket_and_invalid_cors_only_warn(mc, console):
    cors = [{"allowed_origins": [], "allowed_methods": ["GET"]}]

    result = buckets.run([{"name": "docs", "object_lock": True, "cors": cors}], console)

    assert result["failed"] == 0
    assert "Warning: object_lock requested" in output(console)
    assert "Warning: CORS config invalid" in output(console)


def _ilm_ls(mc, *rules):
    """`mc ilm rule ls --json` output (minio-go lifecycle JSON, empty values omitted)."""
    mc.on("ilm", "rule", "ls", stdout=json.dumps(
        {"status": "success", "target": "minio/docs", "config": {"Rules": list(rules)}}))


def _rule(rule_id, prefix, days=None, noncurrent=None):
    rule = {"ID": rule_id, "Status": "Enabled"}
    if prefix:
        rule["Filter"] = {"Prefix": prefix}
    if days:
        rule["Expiration"] = {"Days": days}
    if noncurrent:
        rule["NoncurrentVersionExpiration"] = {"NoncurrentDays": noncurrent}
    return rule


LIFECYCLE = [{"prefix": "daily/", "expire_days": 15}, {"prefix": "", "noncurrent_expire_days": 90}]


def test_matching_lifecycle_rules_are_left_alone(mc, console):
    _ilm_ls(mc, _rule("a1", "daily/", days=15), _rule("b1", "", noncurrent=90))

    result = buckets.run([{"name": "docs", "lifecycle_rules": LIFECYCLE}], console)

    assert result["failed"] == 0
    assert not mc.called("ilm", "rule", "add")
    assert not mc.called("ilm", "rule", "rm")
    assert "2 rule(s) already configured" in output(console)


def test_lifecycle_duplicates_from_earlier_runs_are_removed(mc, console):
    _ilm_ls(mc, _rule("a1", "daily/", days=15), _rule("a2", "daily/", days=15),
            _rule("a3", "daily/", days=15), _rule("b1", "", noncurrent=90))

    result = buckets.run([{"name": "docs", "lifecycle_rules": LIFECYCLE}], console)

    assert result["failed"] == 0
    assert mc.called("ilm", "rule", "rm") == [
        ["ilm", "rule", "rm", "--id", "a2", "minio/docs"],
        ["ilm", "rule", "rm", "--id", "a3", "minio/docs"],
    ]
    assert not mc.called("ilm", "rule", "add")


def test_changed_lifecycle_rule_replaces_the_old_one(mc, console):
    _ilm_ls(mc, _rule("a1", "daily/", days=10), _rule("b1", "", noncurrent=90), _rule("c1", "manual/", days=1))

    result = buckets.run([{"name": "docs", "lifecycle_rules": LIFECYCLE}], console)

    assert result["failed"] == 0
    assert mc.called("ilm", "rule", "rm") == [["ilm", "rule", "rm", "--id", "a1", "minio/docs"]]
    assert mc.called("ilm", "rule", "add") == [
        ["ilm", "rule", "add", "--prefix", "daily/", "--expire-days", "15", "minio/docs"],
    ]


def test_bucket_without_lifecycle_gets_all_rules(mc, console):
    mc.fail("ilm", "rule", "ls", message="Unable to ls lifecycle configuration",
            cause="lifecycle configuration not set")

    result = buckets.run([{"name": "docs", "lifecycle_rules": LIFECYCLE}], console)

    assert result["failed"] == 0
    assert len(mc.called("ilm", "rule", "add")) == 2


def test_lifecycle_rule_that_cannot_be_removed_is_fatal(mc, console):
    _ilm_ls(mc, _rule("a1", "daily/", days=10))
    mc.fail("ilm", "rule", "rm")

    result = buckets.run([{"name": "docs", "lifecycle_rules": LIFECYCLE[:1]}], console)

    assert result["failed"] == 1
    assert not mc.called("ilm", "rule", "add")


# --- policies ---------------------------------------------------------------

def test_policy_that_cannot_be_created_is_fatal(mc, console):
    mc.fail("admin", "policy", "create", message="Unable to create policy", cause="invalid JSON")

    result = policies.run([{"name": "pDocs", "statements": []}], console)

    assert result["failed"] == 1
    assert "Failed: apply policy pDocs" in output(console)


# --- users ------------------------------------------------------------------

@pytest.mark.parametrize("empty", ["access_key", "secret_key"])
def test_user_with_empty_credential_is_skipped_not_failed(mc, console, empty):
    user = {"access_key": "backup", "secret_key": random_secret(), "groups": ["gBackup"], empty: ""}
    context = {}

    result = users.run([user], console, context=context)

    assert result["failed"] == 0
    assert result["items_skipped"] == 1
    assert context["skipped_users"] == {user["access_key"]}
    assert not mc.calls
    assert "Skipped: user" in output(console)


def test_rejected_user_is_fatal(mc, console):
    mc.fail("admin", "user", "add", message="Unable to add new user", cause="The secret key is invalid.")

    result = users.run([{"access_key": "app", "secret_key": "short"}], console, context={})

    assert result["failed"] == 1
    assert "The secret key is invalid" in output(console)


def test_group_membership_and_direct_policy_failures_are_fatal(mc, console):
    mc.fail("admin", "group", "add")
    mc.fail("admin", "policy", "attach", cause=NO_SUCH_POLICY)

    result = users.run(
        [{"access_key": "app", "secret_key": random_secret(), "groups": ["gApp"], "policies": ["pMissing"]}],
        console, context={},
    )

    assert result["failed"] == 2


def test_root_user_is_skipped(mc, console, monkeypatch):
    monkeypatch.setenv("MINIO_ROOT_USER", "admin")

    result = users.run([{"access_key": "admin", "secret_key": random_secret()}], console, context={})

    assert result["failed"] == 0 and result["items_skipped"] == 1
    assert not mc.called("admin", "user", "add")


# --- groups -----------------------------------------------------------------

def _users_then_groups(mc, console, user_items, group_items):
    """Users task, then groups task with one shared context (as main runs them).

    Every group policy attach answers "group does not exist": none of the
    configured users ended up in the group.
    """
    context = {}
    users.run(user_items, console, context=context)
    mc.fail("admin", "policy", "attach", message="Unable to make user/group policy association",
            cause=NO_SUCH_GROUP)
    return groups.run(group_items, console, context=context)


@pytest.mark.parametrize("access_key, has_secret", [("backup", False), ("root", True)],
                         ids=["empty-secret", "root-user"])
def test_group_whose_users_were_all_skipped_is_skipped(mc, console, monkeypatch, access_key, has_secret):
    # The root user case is the built-in default with CONSOLE_USER == MINIO_ROOT_USER.
    monkeypatch.setenv("MINIO_ROOT_USER", "root")
    member = {"access_key": access_key, "secret_key": random_secret() if has_secret else "",
              "groups": ["gBackup"]}

    result = _users_then_groups(mc, console, [member], [{"name": "gBackup", "policies": ["pBackup", "pOther"]}])

    assert result["failed"] == 0
    assert result["items_skipped"] == 1
    assert len(mc.called("admin", "policy", "attach")) == 1
    assert "Skipped: group 'gBackup' has no members: its users were all skipped" in output(console)


@pytest.mark.parametrize("user_items", [
    [{"access_key": "app", "secret_key": random_secret(), "groups": ["gApp"]}],  # typo: gApp vs gAPP
    [{"access_key": "app", "secret_key": random_secret()}],                      # user lists no group
    [],                                                                           # no users configured
], ids=["typo", "not-listed", "no-users"])
def test_group_no_configured_user_is_in_is_fatal(mc, console, user_items):
    result = _users_then_groups(mc, console, user_items, [{"name": "gAPP", "policies": ["pApp"]}])

    assert result["failed"] == 1 and result["items_skipped"] == 0
    assert "Failed: group 'gAPP' does not exist: no configured user is in it" in output(console)


@pytest.mark.parametrize("failing_call", [("admin", "user", "add"), ("admin", "group", "add")])
def test_group_whose_users_could_not_be_added_is_fatal(mc, console, failing_call):
    mc.fail(*failing_call)
    user_items = [
        {"access_key": "app", "secret_key": random_secret(), "groups": ["gApp"]},
        {"access_key": "backup", "secret_key": "", "groups": ["gApp"]},  # skipped does not outweigh failed
    ]

    result = _users_then_groups(mc, console, user_items, [{"name": "gApp", "policies": ["pApp"]}])

    assert result["failed"] == 1 and result["items_skipped"] == 0
    assert "Failed: group 'gApp' does not exist: adding its users failed" in output(console)


def test_group_attach_of_missing_policy_is_fatal(mc, console):
    mc.fail("admin", "policy", "attach", message="Unable to make user/group policy association",
            cause=NO_SUCH_POLICY)

    result = groups.run([{"name": "gApp", "policies": ["pMissing"]}], console)

    assert result["failed"] == 1
    assert "The canned policy does not exist" in output(console)


def test_group_without_policies_is_skipped(mc, console):
    result = groups.run([{"name": "gApp", "policies": []}], console)

    assert result["failed"] == 0 and result["items_skipped"] == 1
    assert not mc.calls


# --- service accounts -------------------------------------------------------

@pytest.fixture
def credentials_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(service_accounts, "CREDENTIALS_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("parent,context", [
    ("backup", {"skipped_users": {"backup"}}),
    ("", {}),
])
def test_service_account_of_skipped_user_is_skipped(mc, console, credentials_dir, parent, context):
    result = service_accounts.run([{"user": parent, "name": "backup-agent"}], console, context=context)

    assert result["failed"] == 0 and result["items_skipped"] == 1
    assert not mc.calls


def test_service_account_creation_failure_is_fatal(mc, console, credentials_dir):
    mc.fail("admin", "user", "svcacct", "add", message="Unable to add service account",
            cause="The specified user does not exist.")

    result = service_accounts.run([{"user": "ghost", "name": "worker"}], console, context={})

    assert result["failed"] == 1
    assert not list(credentials_dir.iterdir())


def test_service_account_with_unreadable_policy_is_fatal(mc, console, credentials_dir):
    mc.fail("admin", "policy", "info", message="Unable to fetch policy", cause=NO_SUCH_POLICY)

    result = service_accounts.run([{"user": "app", "name": "worker", "policy": "pMissing"}], console, context={})

    assert result["failed"] == 1
    assert not mc.called("admin", "user", "svcacct", "add")


def test_service_account_credentials_are_written(mc, console, credentials_dir):
    access_key, secret_key = random_secret(), random_secret()
    mc.on("admin", "user", "svcacct", "add",
          stdout=json.dumps({"status": "success", "accessKey": access_key, "secretKey": secret_key}))

    result = service_accounts.run([{"user": "app", "name": "Worker"}], console, context={})

    assert result["failed"] == 0
    written = json.loads((credentials_dir / "worker.json").read_text())
    assert written == {"user": "app", "name": "Worker", "accessKey": access_key, "secretKey": secret_key}


POLICY_DOC = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:GetObject"],
                                                     "Resource": ["arn:aws:s3:::docs/*"]}]}


def _export_policy(args):
    """What `mc admin policy info ... --policy-file PATH` does: write the raw document."""
    path = args[args.index("--policy-file") + 1]
    with open(path, "w") as f:
        json.dump(POLICY_DOC, f)


def test_service_account_is_scoped_to_its_policy(mc, console, credentials_dir):
    seen = {}
    mc.on("admin", "policy", "info", effect=_export_policy)
    mc.on("admin", "user", "svcacct", "add",
          stdout=json.dumps({"status": "success", "accessKey": random_secret(), "secretKey": random_secret()}),
          effect=lambda args: seen.update(policy=json.loads(Path(args[args.index("--policy") + 1]).read_text())))

    result = service_accounts.run([{"user": "app", "name": "worker", "policy": "pDocs"}], console, context={})

    assert result["failed"] == 0
    assert seen["policy"] == POLICY_DOC
    exported = mc.called("admin", "policy", "info")[0]
    assert exported[:5] == ["admin", "policy", "info", "minio", "pDocs"]
    policy_path = exported[exported.index("--policy-file") + 1]
    assert mc.called("admin", "user", "svcacct", "add")[0][-2:] == ["--policy", policy_path]
    assert not Path(policy_path).exists()


def test_service_account_with_empty_exported_policy_is_fatal(mc, console, credentials_dir):
    result = service_accounts.run([{"user": "app", "name": "worker", "policy": "pDocs"}], console, context={})

    assert result["failed"] == 1
    assert not mc.called("admin", "user", "svcacct", "add")


def test_service_account_without_parsable_credentials_is_fatal(mc, console, credentials_dir):
    mc.on("admin", "user", "svcacct", "add", stdout="unexpected output")

    result = service_accounts.run([{"user": "app", "name": "worker"}], console, context={})

    assert result["failed"] == 1


def _svcacct_list(mc, *access_keys):
    """`mc admin user svcacct list --json`: one compact object per account."""
    lines = [json.dumps({"status": "success", "accessKey": k}) for k in access_keys]
    mc.on("admin", "user", "svcacct", "list", stdout="\n".join(lines))


def _store_credentials(credentials_dir, access_key):
    (credentials_dir / "worker.json").write_text(json.dumps(
        {"user": "app", "name": "worker", "accessKey": access_key, "secretKey": random_secret()}))


def test_service_account_with_valid_stored_credentials_is_kept(mc, console, credentials_dir):
    access_key = random_secret()
    _store_credentials(credentials_dir, access_key)
    before = (credentials_dir / "worker.json").read_text()
    _svcacct_list(mc, random_secret(), access_key)

    result = service_accounts.run([{"user": "app", "name": "worker"}], console, context={})

    assert result["failed"] == 0 and not result["changed"]
    assert not mc.called("admin", "user", "svcacct", "add")
    assert (credentials_dir / "worker.json").read_text() == before


@pytest.mark.parametrize("stored", [True, False])
def test_service_account_is_recreated_when_stored_credentials_are_gone(mc, console, credentials_dir, stored):
    if stored:
        _store_credentials(credentials_dir, random_secret())  # account was deleted in MinIO
    _svcacct_list(mc, random_secret())
    new_key = random_secret()
    mc.on("admin", "user", "svcacct", "add",
          stdout=json.dumps({"status": "success", "accessKey": new_key, "secretKey": random_secret()}))

    result = service_accounts.run([{"user": "app", "name": "worker"}], console, context={})

    assert result["failed"] == 0 and result["changed"]
    assert json.loads((credentials_dir / "worker.json").read_text())["accessKey"] == new_key


def test_service_account_listing_failure_is_fatal(mc, console, credentials_dir):
    mc.fail("admin", "user", "svcacct", "list", message="Unable to list service accounts",
            cause="The specified user does not exist.")

    result = service_accounts.run([{"user": "ghost", "name": "worker"}], console, context={})

    assert result["failed"] == 1
    assert not mc.called("admin", "user", "svcacct", "add")


# --- notifications ----------------------------------------------------------

ENTRY = {
    "id": "cdnpurge",
    "endpoint": "http://minio-worker:8080/webhook",
    "buckets": ["assets"],
    "events": ["put", "delete"],
}
ARN = "arn:minio:sqs:eu-central-1:cdnpurge:webhook"


@pytest.fixture
def notify(monkeypatch, tmp_path):
    monkeypatch.setattr(notifications, "MARKER_DIR", str(tmp_path))
    monkeypatch.setattr(notifications, "_wait_healthy", lambda timeout: True)
    monkeypatch.setattr(notifications, "_endpoint_unreachable", lambda endpoint: False)
    return monkeypatch


def _admin_info(mc, arns):
    mc.on("admin", "info", stdout=json.dumps({"status": "success", "info": {"sqsARN": arns}}))


def test_invalid_notification_id_is_fatal(mc, console, notify):
    result = notifications.run([{**ENTRY, "id": "cdn purge"}], console)

    assert result["failed"] == 1


def test_notification_without_endpoint_is_skipped(mc, console, notify):
    result = notifications.run([{**ENTRY, "endpoint": ""}], console)

    assert result["failed"] == 0 and result["items_skipped"] == 1
    assert not mc.called("admin", "config", "set")


def test_unreachable_endpoint_skips_target_and_bindings(mc, console, notify):
    notify.setattr(notifications, "_endpoint_unreachable", lambda endpoint: True)
    _admin_info(mc, [])
    mc.fail("admin", "config", "set", message="Unable to set server config",
            cause="error (cdnpurge:webhook): dial tcp: lookup minio-worker: no such host")

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 0 and result["items_skipped"] == 1
    assert not mc.called("admin", "service", "restart")
    assert not mc.called("event")
    assert "is not reachable" in output(console)


def test_unreachable_endpoint_is_logged_without_its_credentials(mc, console, notify):
    # A webhook URL may carry userinfo or a token in its query; only host:port is logged.
    user, token = random_secret(), random_secret()
    endpoint = "https://" + user + ":" + token + "@hooks.example.com/notify?token=" + token
    notify.setattr(notifications, "_endpoint_unreachable", lambda endpoint: True)
    _admin_info(mc, [])
    mc.fail("admin", "config", "set")

    notifications.run([{**ENTRY, "endpoint": endpoint}], console)

    log = output(console)
    assert "endpoint hooks.example.com:443 is not reachable" in log
    assert user not in log and token not in log


def test_target_rejected_although_reachable_is_fatal(mc, console, notify):
    _admin_info(mc, [])
    mc.fail("admin", "config", "set", message="Unable to set server config", cause="invalid queue_dir")

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 1
    assert len(mc.called("admin", "config", "set")) == 2  # retried once, as it is reachable
    assert not mc.called("event")


def test_receiver_that_came_up_while_minio_tested_it_is_retried(mc, console, notify):
    _admin_info(mc, [ARN])
    mc.fail("admin", "config", "set", message="Unable to set server config",
            cause="error (cdnpurge:webhook): connection refused", times=1)

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 0 and result["items_skipped"] == 0
    assert len(mc.called("admin", "config", "set")) == 2
    assert mc.called("event", "add")


def test_unhealthy_server_after_restart_is_fatal(mc, console, notify):
    notify.setattr(notifications, "_wait_healthy", lambda timeout: False)
    _admin_info(mc, [])

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 1


def test_target_still_inactive_after_restart_is_fatal(mc, console, notify):
    _admin_info(mc, [])

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 1
    assert not mc.called("event", "add")


def test_target_and_binding_are_applied(mc, console, notify):
    _admin_info(mc, [ARN])  # what the server reports after the restart

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 0
    assert mc.called("event", "add") == [["event", "add", "minio/assets", ARN, "--event", "put,delete"]]


def test_binding_failure_is_fatal(mc, console, notify):
    _admin_info(mc, [ARN])
    mc.fail("event", "add", message="Unable to enable notification", cause="The specified bucket does not exist")

    result = notifications.run([ENTRY], console)

    assert result["failed"] == 1


@pytest.mark.parametrize("endpoint", ["minio-worker:8080", "ftp://minio-worker/hook", "http://:8080/x"])
def test_malformed_endpoint_is_not_mistaken_for_an_unreachable_one(endpoint):
    assert notifications._endpoint_unreachable(endpoint) is False


def test_endpoint_that_refuses_connections_is_unreachable():
    with socket.socket() as probe:  # bind a free port, then close it: nothing listens there
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert notifications._endpoint_unreachable(f"http://127.0.0.1:{port}/hook", attempts=1) is True


def test_endpoint_that_accepts_connections_is_reachable():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        assert notifications._endpoint_unreachable(f"http://127.0.0.1:{port}/hook", attempts=1) is False


def test_bucket_listing_failure_for_wildcard_is_fatal(mc, console, notify):
    _admin_info(mc, [ARN])
    mc.fail("ls")

    result = notifications.run([{**ENTRY, "buckets": ["*"]}], console)

    assert result["failed"] == 1
