"""
Shared helpers for the init tasks: running mc and reporting item outcomes.

The leading underscore keeps main.discover_tasks() from loading this module as
a task.

Outcome contract (see README "Adding New Tasks"):
  - fail()  -> a configured item could not be applied. The task counts it in
               its result's "failed" field and the init container exits 1.
  - skip()  -> an intentionally optional item was left out (e.g. a user whose
               secret is empty). Reported, counted in "items_skipped", never fatal.
  - warn()  -> something worth reading that is neither (e.g. object_lock on an
               existing bucket, CORS declared for an engine that ignores it).
"""

import json
import subprocess

from rich.markup import escape

MC_ALIAS = "minio"


def iter_json(text: str):
    """Yield successive JSON values from mc output.

    mc prints most results as one compact object per line when its output is
    not a terminal, but some subcommands (event ls) and every --json error are
    indented, multi-line documents. raw_decode walks the stream either way.
    """
    decoder = json.JSONDecoder()
    text = text or ""
    idx, n = 0, len(text)
    while idx < n:
        while idx < n and text[idx].isspace():
            idx += 1
        if idx >= n:
            break
        try:
            obj, idx = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            break
        yield obj


def error_text(result: subprocess.CompletedProcess) -> str:
    """Human-readable reason for a failed mc call.

    mc --json reports a failure as a JSON document on stdout:
    {"status": "error", "error": {"message": "...", "cause": {"message": "..."}}}.
    The message names the operation ("Unable to make user/group policy
    association"), the cause carries the server's reason ("The specified group
    does not exist."), so both are kept.
    """
    for obj in iter_json(result.stdout):
        if not isinstance(obj, dict):
            continue
        err = obj.get("error")
        if isinstance(err, str) and err.strip():
            return err.strip()
        if isinstance(err, dict):
            parts = [err.get("message")]
            cause = err.get("cause")
            if isinstance(cause, dict):
                parts.append(cause.get("message"))
            text = " - ".join(p.strip().rstrip(".") for p in parts if isinstance(p, str) and p.strip())
            if text:
                return text
    stderr = (result.stderr or "").strip()
    return stderr or f"mc exited with code {result.returncode}"


def mc(args: list, use_json: bool = True) -> subprocess.CompletedProcess:
    """Run mc; on failure, stderr holds the readable reason (see error_text)."""
    cmd = ["mc"] + (["--json"] if use_json else []) + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        result.stderr = error_text(result)
    return result


def fail(console, text: str) -> None:
    console.print(f"    [red]Failed: {escape(text)}[/]")


def skip(console, text: str) -> None:
    console.print(f"    [yellow]Skipped: {escape(text)}[/]")


def warn(console, text: str) -> None:
    console.print(f"    [yellow]Warning: {escape(text)}[/]")
