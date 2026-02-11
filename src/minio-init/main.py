#!/usr/bin/env python3
"""
MinIO Init - Declarative Object Storage Initialization

Reads JSON configuration files and applies them to a MinIO server
using the mc (MinIO Client) CLI. Designed to be idempotent - safe
to run on every container start.

Configuration loading order:
  1. Built-in default (/app/config/default.json) - always processed
  2. User config - optional, loaded from:
     a) MINIO_INIT_CONFIG env var (if set and file exists)
     b) /app/config/init.json (fallback, if mounted)

JSON values may contain ${ENV_VAR} placeholders for secret injection.
"""

import json
import os
import re
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

console = Console()

MC_ALIAS = "minio"
DEFAULT_CONFIG = "/app/config/default.json"
FALLBACK_USER_CONFIG = "/app/config/init.json"


def get_minio_config() -> dict:
    """Get MinIO connection configuration from environment variables."""
    return {
        "endpoint": os.environ.get("MINIO_ENDPOINT", "http://minio-server:9000"),
        "root_user": os.environ.get("MINIO_ROOT_USER", "minioadmin"),
        "root_password": os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin"),
    }


def wait_for_minio(config: dict, timeout: int = 60) -> bool:
    """Wait for MinIO server to become available."""
    console.print("[dim]Waiting for MinIO server...[/]")

    start_time = time.time()
    last_error = None

    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ["curl", "-sf", f"{config['endpoint']}/minio/health/live"],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                console.print("[green]MinIO server is ready[/]")
                return True
        except Exception as e:
            last_error = e
        time.sleep(2)

    console.print(f"[red]MinIO connection timeout after {timeout}s: {last_error}[/]")
    return False


def setup_mc_alias(config: dict) -> bool:
    """Configure mc alias for the MinIO server."""
    result = run_mc([
        "alias", "set", MC_ALIAS,
        config["endpoint"],
        config["root_user"],
        config["root_password"],
    ])
    return result.returncode == 0


def run_mc(args: list, check: bool = False) -> subprocess.CompletedProcess:
    """Execute an mc command."""
    cmd = ["mc", "--json"] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
    )


def resolve_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} patterns with environment variable values."""
    def replacer(match):
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable '{var_name}' is not set")
        return env_value

    return re.sub(r"\$\{([^}]+)}", replacer, value)


def resolve_config_values(obj):
    """Recursively resolve environment variables in config values."""
    if isinstance(obj, str):
        return resolve_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: resolve_config_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_config_values(item) for item in obj]
    return obj


def load_config(config_path: str) -> dict | None:
    """Load and resolve a JSON configuration file.

    Returns None if the file does not exist.
    """
    path = Path(config_path)
    if not path.exists():
        return None

    with open(path) as f:
        raw_config = json.load(f)

    return resolve_config_values(raw_config)


def discover_configs() -> list[tuple[str, dict]]:
    """Discover and load configuration files in order.

    Returns:
        List of (label, config_dict) tuples.
    """
    configs = []

    # 1. Always load built-in default
    default = load_config(DEFAULT_CONFIG)
    if default:
        configs.append(("default", default))
    else:
        console.print(f"[yellow]Warning: Built-in default not found: {DEFAULT_CONFIG}[/]")

    # 2. Load user config (env var takes precedence, fallback to standard path)
    user_config_path = os.environ.get("MINIO_INIT_CONFIG", FALLBACK_USER_CONFIG)
    if user_config_path != DEFAULT_CONFIG and Path(user_config_path).exists():
        user_config = load_config(user_config_path)
        if user_config:
            configs.append(("user", user_config))

    return configs


def discover_tasks() -> list:
    """Discover available initialization tasks from the tasks/ directory."""
    tasks_dir = Path(__file__).parent / "tasks"
    tasks = []

    for task_file in sorted(tasks_dir.glob("*.py")):
        if task_file.name.startswith("_"):
            continue

        module_name = f"tasks.{task_file.stem}"
        try:
            module = import_module(module_name)
            if hasattr(module, "run"):
                tasks.append({
                    "name": getattr(module, "TASK_NAME", task_file.stem),
                    "description": getattr(module, "TASK_DESCRIPTION", ""),
                    "config_key": getattr(module, "CONFIG_KEY", None),
                    "module": module,
                })
        except Exception as e:
            console.print(f"[yellow]Warning: Failed to load task {task_file.name}: {e}[/]")

    return tasks


def process_config(label: str, config: dict, tasks: list) -> tuple[int, int, int]:
    """Process a single config through all tasks.

    Returns:
        Tuple of (applied, skipped, failed) counts.
    """
    applied = 0
    skipped = 0
    failed = 0

    for task in tasks:
        task_name = task["name"]
        config_key = task["config_key"]

        # Skip tasks with no config data
        if config_key and not config.get(config_key):
            skipped += 1
            continue

        console.print(f"[bold]> {task_name}[/]")
        if task["description"]:
            console.print(f"  [dim]{task['description']}[/]")

        try:
            items = config.get(config_key, []) if config_key else []
            result = task["module"].run(items, console, config=config)

            if result.get("skipped"):
                console.print(f"  [dim]Skipped: {result.get('message', 'Not applicable')}[/]")
                skipped += 1
            elif result.get("changed"):
                console.print(f"  [green]+ {result.get('message', 'Done')}[/]")
                applied += 1
            else:
                console.print(f"  [blue]= {result.get('message', 'Already configured')}[/]")
                applied += 1

        except Exception as e:
            console.print(f"  [red]x Failed: {e}[/]")
            failed += 1

        console.print()

    return applied, skipped, failed


def main() -> int:
    """Main entry point."""
    console.print(Panel.fit(
        "[bold blue]MinIO Init[/]\n"
        "[dim]Declarative Object Storage Initialization[/]",
        border_style="blue",
    ))
    console.print()

    # Get MinIO configuration
    minio_config = get_minio_config()

    if not minio_config["root_password"]:
        console.print("[red]Error: MINIO_ROOT_PASSWORD not set[/]")
        return 1

    console.print(f"[dim]Endpoint: {minio_config['endpoint']}[/]")
    console.print()

    # Wait for MinIO server
    timeout = int(os.environ.get("MINIO_WAIT_TIMEOUT", "60"))
    if not wait_for_minio(minio_config, timeout):
        return 1

    # Configure mc alias
    console.print("[dim]Configuring MinIO client...[/]")
    if not setup_mc_alias(minio_config):
        console.print("[red]Error: Failed to configure mc alias[/]")
        return 1

    console.print("[green]MinIO client configured[/]")
    console.print()

    # Discover tasks
    tasks = discover_tasks()
    if not tasks:
        console.print("[yellow]No initialization tasks found[/]")
        return 0

    # Discover and load configs
    try:
        configs = discover_configs()
    except (ValueError, json.JSONDecodeError) as e:
        console.print(f"[red]Error loading config: {e}[/]")
        return 1

    if not configs:
        console.print("[yellow]No configuration files found[/]")
        return 0

    # Process each config through all tasks
    total_applied = 0
    total_skipped = 0
    total_failed = 0

    for label, config in configs:
        console.print(f"[bold cyan]── Processing {label} configuration ──[/]")
        console.print()

        applied, skipped, failed = process_config(label, config, tasks)
        total_applied += applied
        total_skipped += skipped
        total_failed += failed

    # Summary
    console.print("─" * 50)

    if total_failed == 0:
        console.print(
            f"[green]Initialization complete "
            f"({total_applied} applied, {total_skipped} skipped)[/]"
        )
        return 0
    else:
        console.print(
            f"[red]Initialization had errors "
            f"({total_failed} failed, {total_applied} applied, {total_skipped} skipped)[/]"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
