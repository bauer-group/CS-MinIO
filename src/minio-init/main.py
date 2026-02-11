#!/usr/bin/env python3
"""
MinIO Init - Declarative Object Storage Initialization

Reads a JSON configuration file and applies it to a MinIO server
using the mc (MinIO Client) CLI. Designed to be idempotent - safe
to run on every container start.

Supports: buckets, policies, groups, users, and service accounts.
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


def get_minio_config() -> dict:
    """Get MinIO connection configuration from environment variables."""
    return {
        "endpoint": os.environ.get("MINIO_ENDPOINT", "http://minio-server:9000"),
        "root_user": os.environ.get("MINIO_ROOT_USER", "admin"),
        "root_password": os.environ.get("MINIO_ROOT_PASSWORD", ""),
    }


def wait_for_minio(config: dict, timeout: int = 60) -> bool:
    """Wait for MinIO server to become available.

    Args:
        config: MinIO connection configuration.
        timeout: Maximum seconds to wait.

    Returns:
        True if MinIO is available, False on timeout.
    """
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
    """Configure mc alias for the MinIO server.

    Args:
        config: MinIO connection configuration.

    Returns:
        True if alias was configured successfully.
    """
    result = run_mc([
        "alias", "set", MC_ALIAS,
        config["endpoint"],
        config["root_user"],
        config["root_password"],
    ])
    return result.returncode == 0


def run_mc(args: list, check: bool = False) -> subprocess.CompletedProcess:
    """Execute an mc command.

    Args:
        args: Arguments to pass to mc.
        check: If True, raise on non-zero exit code.

    Returns:
        CompletedProcess result.
    """
    cmd = ["mc", "--json"] + args
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=check,
    )


def resolve_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} patterns with environment variable values.

    Args:
        value: String potentially containing ${VAR} placeholders.

    Returns:
        String with placeholders resolved.

    Raises:
        ValueError: If a referenced environment variable is not set.
    """
    def replacer(match):
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable '{var_name}' is not set")
        return env_value

    return re.sub(r"\$\{([^}]+)}", replacer, value)


def resolve_config_values(obj):
    """Recursively resolve environment variables in config values.

    Args:
        obj: JSON-parsed object (dict, list, or scalar).

    Returns:
        Object with all string values resolved.
    """
    if isinstance(obj, str):
        return resolve_env_vars(obj)
    elif isinstance(obj, dict):
        return {k: resolve_config_values(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [resolve_config_values(item) for item in obj]
    return obj


def load_config(config_path: str) -> dict:
    """Load and resolve a JSON configuration file.

    Args:
        config_path: Path to the JSON config file.

    Returns:
        Parsed and resolved configuration dict.
    """
    path = Path(config_path)
    if not path.exists():
        console.print(f"[yellow]Config file not found: {config_path}[/]")
        console.print("[dim]Using empty default configuration[/]")
        return {"buckets": [], "policies": [], "groups": [], "users": [], "service_accounts": []}

    with open(path) as f:
        raw_config = json.load(f)

    return resolve_config_values(raw_config)


def discover_tasks() -> list:
    """Discover available initialization tasks from the tasks/ directory.

    Returns:
        Sorted list of task dicts with name, description, and module.
    """
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


def main() -> int:
    """Main entry point.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    console.print(Panel.fit(
        "[bold blue]MinIO Init[/]\n"
        "[dim]Declarative Object Storage Initialization[/]",
        border_style="blue",
    ))
    console.print()

    # Get MinIO configuration
    config = get_minio_config()

    if not config["root_password"]:
        console.print("[red]Error: MINIO_ROOT_PASSWORD not set[/]")
        return 1

    console.print(f"[dim]Endpoint: {config['endpoint']}[/]")
    console.print()

    # Wait for MinIO server
    timeout = int(os.environ.get("MINIO_WAIT_TIMEOUT", "60"))
    if not wait_for_minio(config, timeout):
        return 1

    # Configure mc alias
    console.print("[dim]Configuring MinIO client...[/]")
    if not setup_mc_alias(config):
        console.print("[red]Error: Failed to configure mc alias[/]")
        return 1

    console.print("[green]MinIO client configured[/]")
    console.print()

    # Load configuration
    config_path = os.environ.get("MINIO_INIT_CONFIG", "/app/config/init.json")
    try:
        init_config = load_config(config_path)
    except ValueError as e:
        console.print(f"[red]Error resolving config: {e}[/]")
        return 1
    except json.JSONDecodeError as e:
        console.print(f"[red]Error parsing config JSON: {e}[/]")
        return 1

    console.print(f"[dim]Config: {config_path}[/]")
    console.print()

    # Discover and run tasks
    tasks = discover_tasks()

    if not tasks:
        console.print("[yellow]No initialization tasks found[/]")
        return 0

    console.print(f"[bold]Found {len(tasks)} initialization task(s)[/]")
    console.print()

    failed = 0
    skipped = 0

    for task in tasks:
        task_name = task["name"]
        config_key = task["config_key"]

        # Skip tasks with no config data
        if config_key and not init_config.get(config_key):
            console.print(f"[dim]- {task_name}: Skipped (no configuration)[/]")
            skipped += 1
            continue

        console.print(f"[bold]> {task_name}[/]")
        if task["description"]:
            console.print(f"  [dim]{task['description']}[/]")

        try:
            items = init_config.get(config_key, []) if config_key else []
            result = task["module"].run(items, console)

            if result.get("skipped"):
                console.print(f"  [dim]- Skipped: {result.get('message', 'Not applicable')}[/]")
                skipped += 1
            elif result.get("changed"):
                console.print(f"  [green]+ Applied: {result.get('message', 'Done')}[/]")
            else:
                console.print(f"  [blue]= No changes: {result.get('message', 'Already configured')}[/]")

        except Exception as e:
            console.print(f"  [red]x Failed: {e}[/]")
            failed += 1

        console.print()

    # Summary
    console.print("-" * 50)
    total = len(tasks)
    success = total - failed - skipped

    if failed == 0:
        console.print(f"[green]Initialization complete ({success} applied, {skipped} skipped)[/]")
        return 0
    else:
        console.print(f"[red]Initialization failed ({failed} errors, {success} applied, {skipped} skipped)[/]")
        return 1


if __name__ == "__main__":
    sys.exit(main())
