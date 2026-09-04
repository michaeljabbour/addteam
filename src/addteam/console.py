"""Shared console instances.

`console` writes to stdout (results, reports).
`err_console` writes to stderr (errors, warnings, notices) so that stdout
stays clean for piping and --json output.
"""

from __future__ import annotations

from rich.console import Console

console = Console()
err_console = Console(stderr=True)


def error(message: str) -> None:
    """Print an error message to stderr."""
    err_console.print(f"[red]error:[/red] {message}")


def warning(message: str) -> None:
    """Print a warning message to stderr."""
    err_console.print(f"[yellow]warning:[/yellow] {message}")
