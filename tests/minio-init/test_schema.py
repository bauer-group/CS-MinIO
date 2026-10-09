"""init.schema.json: the URL consumers put in "$schema" must resolve to a schema
that accepts the documented configuration."""

import json
from pathlib import Path

import pytest
from conftest import SRC, load_task
from jsonschema import Draft7Validator

ROOT = SRC.parents[1]
SCHEMA = json.loads((ROOT / "init.schema.json").read_text(encoding="utf-8"))
SCHEMA_URL = "https://raw.githubusercontent.com/bauer-group/CS-MinIO/main/init.schema.json"
VALIDATOR = Draft7Validator(SCHEMA)


def _errors(config: dict) -> list[str]:
    return [error.message for error in VALIDATOR.iter_errors(config)]


def test_schema_is_a_valid_draft_07_schema_published_at_the_referenced_url():
    Draft7Validator.check_schema(SCHEMA)
    assert SCHEMA["$id"] == SCHEMA_URL


@pytest.mark.parametrize("path", [
    ROOT / "config" / "minio-init.example.json",
    SRC / "config" / "default.json",
])
def test_shipped_configs_validate(path: Path):
    config = json.loads(path.read_text(encoding="utf-8"))
    assert _errors(config) == []


def test_example_config_references_the_schema():
    config = json.loads((ROOT / "config" / "minio-init.example.json").read_text(encoding="utf-8"))
    assert config["$schema"] == SCHEMA_URL


@pytest.mark.parametrize("stem", ["01_buckets", "02_policies", "03_users", "04_groups",
                                  "05_service_accounts", "06_notifications"])
def test_every_task_config_key_is_described(stem):
    assert load_task(stem).CONFIG_KEY in SCHEMA["properties"]


def test_comment_keys_are_allowed_everywhere():
    config = {
        "_description": "comment",
        "buckets": [{"name": "docs", "_why": "comment", "lifecycle_rules": [{"expire_days": 1, "_note": "x"}]}],
    }
    assert _errors(config) == []


@pytest.mark.parametrize("config", [
    {"bucket": []},                                                   # typo in a top-level key
    {"buckets": [{"name": "docs", "policy": "download"}]},            # mc vocabulary, not ours
    {"buckets": [{"name": "docs", "lifecycle_rules": [{"prefix": "x/"}]}]},  # rule without an action
    {"users": [{"access_key": "app"}]},                               # secret_key missing
    {"notifications": [{"id": "cdn purge", "endpoint": "http://w:8080"}]},
])
def test_schema_flags_configuration_errors(config):
    assert _errors(config)
