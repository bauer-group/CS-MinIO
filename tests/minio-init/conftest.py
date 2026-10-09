"""Shared fixtures for the minio-init unit tests.

The tests never start mc or MinIO: `FakeMC` replaces subprocess.run and answers
each mc call from a small rule table, so every failure path can be provoked
deterministically. Secrets are generated per run (never literals in the repo).
"""

import importlib
import json
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console

SRC = Path(__file__).resolve().parents[2] / "src" / "minio-init"
sys.path.insert(0, str(SRC))


def load_task(stem: str):
    """Import a task module by file stem, e.g. "03_users"."""
    return importlib.import_module(f"tasks.{stem}")


def mc_error(message: str, cause: str = "") -> str:
    """stdout of a failed `mc --json` call - an indented document, as mc prints it."""
    error = {"message": message, "cause": {"message": cause, "error": {}}, "type": "fatal"}
    return json.dumps({"status": "error", "error": error}, indent=1)


def random_secret() -> str:
    return secrets.token_hex(12)


class FakeMC:
    """Stands in for subprocess.run; records every mc call (without "--json")."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self._rules: list[dict] = []

    def on(self, *prefix: str, rc: int = 0, stdout: str = "", effect=None, times: int | None = None) -> "FakeMC":
        """Answer calls whose arguments start with `prefix` (later rules win).

        `effect(args)` runs on a match, e.g. to write the file mc would write.
        `times` limits how often the rule answers before it is dropped.
        """
        self._rules.insert(0, {"prefix": prefix, "rc": rc, "stdout": stdout, "effect": effect, "times": times})
        return self

    def fail(self, *prefix: str, message: str = "Unable to complete the request", cause: str = "",
             times: int | None = None) -> "FakeMC":
        return self.on(*prefix, rc=1, stdout=mc_error(message, cause), times=times)

    def called(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def __call__(self, cmd, *args, **kwargs):
        assert cmd[0] == "mc", f"unexpected command {cmd}"
        mc_args = [a for a in cmd[1:] if a != "--json"]
        self.calls.append(mc_args)
        for rule in self._rules:
            if tuple(mc_args[: len(rule["prefix"])]) == rule["prefix"]:
                if rule["times"] is not None:
                    rule["times"] -= 1
                    if rule["times"] == 0:
                        self._rules.remove(rule)
                if rule["effect"]:
                    rule["effect"](mc_args)
                # mc prints the error document on stdout and only a newline on stderr
                rc = rule["rc"]
                return subprocess.CompletedProcess(cmd, rc, stdout=rule["stdout"], stderr="\n" if rc else "")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture
def mc(monkeypatch):
    fake = FakeMC()
    helper = importlib.import_module("tasks._mc")
    monkeypatch.setattr(helper.subprocess, "run", fake)
    return fake


@pytest.fixture
def console():
    return Console(record=True, width=240, color_system=None)


def output(console: Console) -> str:
    return console.export_text(clear=False)
