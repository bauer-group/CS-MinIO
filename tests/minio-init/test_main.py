"""Exit status of the init container (mc is faked, see conftest)."""

import json
from types import SimpleNamespace

import pytest
from conftest import SRC, random_secret

import main


def _task(result=None, error=None):
    def run(items, console, **kwargs):
        if error:
            raise error
        return result

    return {"name": "Fake", "description": "", "config_key": "things", "module": SimpleNamespace(run=run)}


def test_discover_tasks_loads_all_tasks_in_order():
    names = [t["name"] for t in main.discover_tasks()]
    assert names == ["Buckets", "Policies", "Users", "Groups", "Service Accounts", "Notifications"]


def test_a_task_that_cannot_be_loaded_is_fatal(monkeypatch):
    real_import = main.import_module

    def broken(name):
        if name == "tasks.03_users":
            raise SyntaxError("broken")
        return real_import(name)

    monkeypatch.setattr(main, "import_module", broken)
    with pytest.raises(RuntimeError, match="03_users.py"):
        main.discover_tasks()


@pytest.mark.parametrize("task,expected", [
    (_task({"changed": True, "message": "ok"}), (1, 0, 0, 0)),
    (_task({"changed": True, "message": "ok", "items_skipped": 2}), (1, 0, 0, 2)),
    (_task({"changed": True, "message": "2 failed", "failed": 2}), (0, 0, 2, 0)),
    (_task(error=ValueError("boom")), (0, 0, 1, 0)),
])
def test_process_config_counts_failed_items(task, expected):
    assert main.process_config("user", {"things": [1]}, [task]) == expected


@pytest.fixture
def run_main(monkeypatch, tmp_path, mc):
    """Run main() against the built-in default config plus `user_config`."""
    monkeypatch.setattr(main, "wait_for_minio", lambda config, timeout: True)
    monkeypatch.setattr(main, "setup_mc_alias", lambda config: True)
    monkeypatch.setattr(main, "DEFAULT_CONFIG", str(SRC / "config" / "default.json"))
    monkeypatch.setenv("MINIO_ROOT_USER", "root")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", random_secret())
    monkeypatch.setenv("CONSOLE_USER", "console-admin")
    monkeypatch.setenv("CONSOLE_PASSWORD", random_secret())
    sa_module = __import__("tasks.05_service_accounts", fromlist=["CREDENTIALS_DIR"])
    monkeypatch.setattr(sa_module, "CREDENTIALS_DIR", str(tmp_path / "credentials"))

    def run(user_config: dict) -> int:
        path = tmp_path / "init.json"
        path.write_text(json.dumps(user_config))
        monkeypatch.setenv("MINIO_INIT_CONFIG", str(path))
        return main.main()

    return run


BACKUP_CONFIG = {
    "policies": [{"name": "pBackup", "statements": []}],
    "users": [{"access_key": "backup", "secret_key": "${BACKUP_PASSWORD}", "groups": ["gBackup"]}],
    "groups": [{"name": "gBackup", "policies": ["pBackup"]}],
    "service_accounts": [{"user": "backup", "name": "backup-agent"}],
}


def test_optional_backup_user_without_secret_exits_zero(run_main, mc, monkeypatch, capsys):
    monkeypatch.setenv("BACKUP_PASSWORD", "")
    mc.fail("admin", "policy", "attach", "minio", "pBackup", "--group", "gBackup",
            message="Unable to make user/group policy association",
            cause="The specified group does not exist.")

    assert run_main(BACKUP_CONFIG) == 0
    assert not mc.called("admin", "user", "svcacct")
    out = capsys.readouterr().out
    assert "3 optional item(s) skipped" in out
    assert "Failed" not in out


def test_any_failed_item_exits_one(run_main, mc, capsys):
    mc.fail("admin", "policy", "attach", "minio", "pMissing",
            message="Unable to make user/group policy association",
            cause="The canned policy does not exist.")

    config = {"groups": [{"name": "gAdministrators", "policies": ["pMissing"]}]}
    assert run_main(config) == 1
    out = capsys.readouterr().out
    assert "Failed: attach policy pMissing to group gAdministrators" in out
    assert "Initialization had errors (1 failed" in out


def test_missing_environment_variable_is_fatal(run_main, monkeypatch):
    monkeypatch.delenv("BACKUP_PASSWORD", raising=False)

    assert run_main(BACKUP_CONFIG) == 1
