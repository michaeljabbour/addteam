"""Terminal presentation: header, config summary, update notice, prompts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
from rich.markup import escape
from rich.prompt import Confirm
from rich.text import Text

from . import __version__
from .console import console, err_console

_UPDATE_CHECK_INTERVAL_S = 24 * 60 * 60


def _update_cache_path() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_home) if cache_home else Path.home() / ".cache"
    return base / "addteam" / "update-check.json"


def _parse_version(value: str) -> tuple[int, ...]:
    """Lenient version parse: "1.2.0b1" -> (1, 2, 0)."""
    parts: list[int] = []
    for chunk in value.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_updates() -> None:
    """Check PyPI for a newer version and notify, at most once per 24h.

    Skipped entirely in CI, for non-TTY output, local/dev installs, or when
    ADDTEAM_NO_UPDATE_CHECK is set. Never interrupts the user on failure.
    """
    if os.environ.get("ADDTEAM_NO_UPDATE_CHECK") or os.environ.get("CI"):
        return
    if not err_console.is_terminal or __version__.startswith("0.0.0"):
        return

    cache_path = _update_cache_path()
    latest: str | None = None

    try:
        cached = json.loads(cache_path.read_text())
        if time.time() - cached.get("checked_at", 0) < _UPDATE_CHECK_INTERVAL_S:
            latest = cached.get("latest") or None
    except (OSError, ValueError):
        latest = None  # unreadable/corrupt cache — refetch below

    if latest is None:
        try:
            resp = httpx.get("https://pypi.org/pypi/addteam/json", timeout=2)
            if resp.status_code != 200:
                return
            latest = resp.json().get("info", {}).get("version", "") or None
            if latest:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"checked_at": time.time(), "latest": latest}))
        except (httpx.HTTPError, OSError, ValueError):
            return  # Fail silently — never interrupt the user

    if latest and _parse_version(latest) > _parse_version(__version__):
        err_console.print(
            f"  [dim]update available: {__version__} → {latest}  (uvx --refresh addteam | pip install -U addteam)[/dim]"
        )
        err_console.print()


def print_header(repo_name: str, repo_owner: str, me: str, mode: str | None = None) -> None:
    title = Text()
    title.append("addteam", style="bold magenta")
    title.append(f" v{__version__}", style="dim")
    if mode:
        title.append(f"  [{mode}]", style="bold yellow")

    console.print()
    console.print(title)
    console.print()
    if me == repo_owner:
        console.print(f"  [bold]{repo_owner}/{repo_name}[/bold] [dim](you)[/dim]")
    else:
        console.print(f"  [bold]{repo_owner}/{repo_name}[/bold]")
        console.print(f"  [dim]acting as {me}[/dim]")
    console.print()


def print_config(
    source: str,
    target: str,
    default_perm: str,
    sync: bool,
    user_count: int,
    welcome: bool,
    warnings: list[str] | None = None,
    groups: list[str] | None = None,
) -> None:
    console.print(f"  [dim]source[/dim]      {escape(source)}")
    console.print(f"  [dim]target[/dim]      {target}")
    console.print(f"  [dim]permission[/dim]  {default_perm}")
    if groups:
        console.print(f"  [dim]groups[/dim]      {', '.join(groups)}")
    if sync:
        console.print("  [dim]mode[/dim]        sync (will remove unlisted)")
    console.print(f"  [dim]welcome[/dim]     {'create issues for new users' if welcome else 'off'}")
    console.print(f"  [dim]users[/dim]       {user_count}")
    console.print()
    for note in warnings or []:
        err_console.print(f"[yellow]warning:[/yellow] {note}")
    if warnings:
        console.print()


def print_separator() -> None:
    console.print("  " + "─" * 50, style="dim")
    console.print()


def confirm_removals(repo_full_name: str, count: int) -> bool:
    """Ask the user to confirm a destructive sync removal."""
    return Confirm.ask(
        f"  Remove {count} collaborator(s) from [bold]{escape(repo_full_name)}[/bold]?",
        default=False,
        console=err_console,
    )
