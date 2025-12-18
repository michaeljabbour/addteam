from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

__version__ = "0.1.0"

console = Console()

FALLBACK_COLLABORATORS_REPO = os.getenv("ADDMADETEAM_FALLBACK_COLLABORATORS_REPO", "michaeljabbour/addteam")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_checked(cmd: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = _run(cmd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing dependency for {what}: {cmd[0]!r} not found") from exc

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Failed to {what}: {details}")
    return result


def _gh_json(args: list[str], *, what: str) -> dict:
    result = _run_checked(["gh", *args], what=what)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected non-JSON output while trying to {what}") from exc


def _gh_text(args: list[str], *, what: str) -> str:
    result = _run_checked(["gh", *args], what=what)
    return result.stdout.strip()


def _parse_usernames(text: str) -> list[str]:
    seen: set[str] = set()
    users: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            line = line[1:]
        if line not in seen:
            seen.add(line)
            users.append(line)
    return users


def _is_valid_repo_spec(value: str) -> bool:
    value = value.strip()
    if not value or value.endswith("/"):
        return False
    parts = value.split("/")
    if len(parts) not in (2, 3):
        return False
    return all(part.strip() for part in parts)


def _split_repo_spec(value: str) -> tuple[str | None, str, str]:
    parts = value.strip().split("/")
    if len(parts) == 2:
        owner, repo = parts
        return None, owner, repo
    if len(parts) == 3:
        host, owner, repo = parts
        return host, owner, repo
    raise ValueError(f"Invalid repo spec: {value!r}")


def _normalize_argv(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    for arg in argv:
        if arg.startswith("--repo") and arg != "--repo" and not arg.startswith("--repo="):
            value = arg[len("--repo") :]
            if value:
                normalized.extend(["--repo", value])
                continue
        if arg.startswith("--provider") and arg != "--provider" and not arg.startswith("--provider="):
            value = arg[len("--provider") :]
            if value:
                normalized.extend(["--provider", value])
                continue
        if arg.startswith("--permission") and arg != "--permission" and not arg.startswith("--permission="):
            value = arg[len("--permission") :]
            if value:
                normalized.extend(["--permission", value])
                continue
        normalized.append(arg)
    return normalized


def _git_root() -> Path | None:
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


def _resolve_local_path(path: str, *, prefer_repo_root: bool) -> Path | None:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate if candidate.exists() else None

    if candidate.exists():
        return candidate

    if prefer_repo_root:
        repo_root = _git_root()
        if repo_root:
            candidate = repo_root / path
            if candidate.exists():
                return candidate

    return None


def _looks_like_local_path(value: str) -> bool:
    value = value.strip()
    if not value:
        return False
    if value.startswith(("~", "/", "./", "../", "\\")):
        return True
    # Windows drive letter, e.g. C:\path or C:/path
    if len(value) >= 3 and value[1] == ":" and value[2] in ("/", "\\"):
        return True
    return False


def _load_usernames(path: Path) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"{path.as_posix()} not found")
    return _parse_usernames(path.read_text())


def _gh_read_repo_file(repo_owner: str, repo_name: str, path: str, *, hostname: str | None = None) -> str:
    cmd = [
        "gh",
        "api",
        "-X",
        "GET",
        "-H",
        "Accept: application/vnd.github.raw",
        f"repos/{repo_owner}/{repo_name}/contents/{path}",
    ]
    if hostname:
        cmd[2:2] = ["--hostname", hostname]
    result = _run_checked(
        cmd,
        what=f"read {path} from repo",
    )
    return result.stdout


def _get_collaborators(repo_owner: str, repo_name: str) -> set[str]:
    result = _run_checked(
        [
            "gh",
            "api",
            "-X",
            "GET",
            f"repos/{repo_owner}/{repo_name}/collaborators",
            "--paginate",
            "--jq",
            ".[].login",
            "-f",
            "affiliation=direct",
        ],
        what="fetch collaborators",
    )
    return set(line.strip() for line in result.stdout.splitlines() if line.strip())


def _http_post_json(url: str, *, headers: dict[str, str], payload: dict, timeout_s: int = 30) -> dict:
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_s)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"HTTP {exc.response.status_code} from {url}: {exc.response.text}") from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"Network error calling {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {resp.text[:200]}") from exc


def _generate_repo_summary(
    *, provider: str, repo_full_name: str, repo_description: str, first_use_cmd: str, timeout_s: int = 30
) -> str:
    prompt = "\n".join(
        [
            "In 2–3 short sentences, describe this GitHub repository for a collaborator.",
            "",
            f"Repo: {repo_full_name}",
            f"Existing description: {repo_description or '(none)'}",
            "",
            "Include:",
            "- what it does",
            "- the fastest path to first use of the tool",
            "",
            "Requirements:",
            f"- Include this exact command in the answer: {first_use_cmd}",
            "- Put the command on its own line and do not add line breaks inside it.",
            "- Mention that `gh` must be installed + authenticated.",
            "- Keep it crisp and practical (no generic advice like 'clone the repo').",
        ]
    )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        response = _http_post_json(
            "https://api.openai.com/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            payload={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 120,
                "temperature": 0.2,
            },
            timeout_s=timeout_s,
        )
        try:
            return response["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected OpenAI response: {response}") from exc

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")

        response = _http_post_json(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            payload={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 120,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout_s=timeout_s,
        )
        try:
            return response["content"][0]["text"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected Anthropic response: {response}") from exc

    raise RuntimeError(f"Unknown provider: {provider}")


def _write_readme_summary(readme_path: Path, summary: str) -> None:
    begin = "<!-- BEGIN AUTO SUMMARY -->"
    end = "<!-- END AUTO SUMMARY -->"

    if readme_path.exists():
        existing = readme_path.read_text()
    else:
        existing = ""

    block = f"{begin}\n\n{summary.strip()}\n\n{end}\n"

    if begin in existing and end in existing:
        before = existing.split(begin, 1)[0]
        after = existing.split(end, 1)[1]
        readme_path.write_text(before + block + after.lstrip("\n"))
        return

    if existing.strip():
        readme_path.write_text(existing.rstrip() + "\n\n" + block)
    else:
        readme_path.write_text(block)


def _resolve_collaborators(
    collab_spec: str, repo_owner: str, repo_name: str
) -> tuple[list[str], str]:
    """
    Resolve collaborators from the spec.
    Returns (users, source_description).
    
    Resolution order:
    1. If spec starts with 'repo:' → read from target repo
    2. If spec starts with 'local:' → read from local file only
    3. Otherwise:
       a. Try local file first
       b. If not found, try target repo
       c. If not found, try fallback repo
    """
    repo_full_name = f"{repo_owner}/{repo_name}"
    
    # Explicit repo: prefix
    if collab_spec.startswith("repo:"):
        repo_path = collab_spec.removeprefix("repo:").lstrip("/")
        if not repo_path:
            raise ValueError("repo path is empty")
        users = _parse_usernames(_gh_read_repo_file(repo_owner, repo_name, repo_path))
        return users, f"{repo_full_name}:{repo_path}"

    # Explicit local: prefix
    local_path = collab_spec
    if collab_spec.startswith("local:"):
        local_path = collab_spec.removeprefix("local:")
        if not local_path:
            raise ValueError("local path is empty")
        resolved = _resolve_local_path(local_path, prefer_repo_root=True)
        if not resolved:
            raise FileNotFoundError(f"local file not found: {local_path}")
        users = _load_usernames(resolved)
        return users, f"local:{resolved}"

    # Auto-resolve: try local first
    resolved = _resolve_local_path(local_path, prefer_repo_root=True)
    if resolved:
        users = _load_usernames(resolved)
        return users, f"local:{resolved}"

    # If it looks like a local path, don't try repo fallback
    if _looks_like_local_path(local_path):
        raise FileNotFoundError(f"local file not found: {local_path}")

    # Try target repo
    repo_path = collab_spec.lstrip("/")
    try:
        users = _parse_usernames(_gh_read_repo_file(repo_owner, repo_name, repo_path))
        return users, f"{repo_full_name}:{repo_path}"
    except RuntimeError as exc:
        if "HTTP 404" not in str(exc):
            raise

    # Try fallback repo
    fallback_spec = FALLBACK_COLLABORATORS_REPO
    if not _is_valid_repo_spec(fallback_spec) or fallback_spec == repo_full_name:
        raise FileNotFoundError(f"collaborators file not found: {collab_spec}")

    host, fallback_owner, fallback_repo = _split_repo_spec(fallback_spec)
    users = _parse_usernames(
        _gh_read_repo_file(fallback_owner, fallback_repo, repo_path, hostname=host)
    )
    return users, f"{fallback_owner}/{fallback_repo}:{repo_path} (fallback)"


def _print_header(repo_name: str, repo_owner: str, me: str, dry_run: bool) -> None:
    """Print a clean header with context."""
    title = Text()
    title.append("addteam", style="bold magenta")
    title.append(f" v{__version__}", style="dim")
    if dry_run:
        title.append("  [dry-run]", style="bold yellow")
    
    console.print()
    console.print(title)
    console.print()
    console.print(f"  [bold]{repo_name}[/bold] [dim]({repo_owner})[/dim]")
    console.print(f"  [dim]authenticated as[/dim] {me}")
    console.print()


def _print_config(source: str, permission: str, sync: bool, user_count: int) -> None:
    """Print configuration summary."""
    console.print(f"  [dim]source[/dim]      {source}")
    console.print(f"  [dim]permission[/dim]  {permission}")
    if sync:
        console.print(f"  [dim]mode[/dim]        sync (will remove unlisted users)")
    console.print(f"  [dim]users[/dim]       {user_count}")
    console.print()


def _print_separator() -> None:
    """Print a subtle separator."""
    console.print("  " + "─" * 50, style="dim")
    console.print()


def run(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _normalize_argv(argv)

    parser = argparse.ArgumentParser(
        prog="addteam",
        description="Bootstrap repo collaborators + optional AI summary.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  addteam                     # run in current repo
  addteam -r owner/repo       # target specific repo
  addteam -u octocat          # invite single user
  addteam -n                  # dry-run (preview)
  addteam -s                  # sync mode (remove unlisted)
  addteam -f team.txt         # use custom file
""",
    )
    parser.add_argument(
        "-f", "--collaborators-file",
        default="collaborators.txt",
        metavar="FILE",
        help="Collaborators file. Prefixes: 'local:' or 'repo:' (default: collaborators.txt)",
    )
    parser.add_argument(
        "-u", "--user",
        metavar="NAME",
        help="Invite a single GitHub user (skips file)",
    )
    parser.add_argument(
        "-p", "--permission",
        default="push",
        choices=["pull", "triage", "push", "maintain", "admin"],
        help="Permission level (default: push)",
    )
    parser.add_argument(
        "-r", "--repo",
        metavar="OWNER/REPO",
        help="Target repo (default: current directory)",
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Preview without making changes",
    )
    parser.add_argument(
        "-s", "--sync",
        action="store_true",
        help="Remove collaborators not in the list",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip AI-generated summary",
    )
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "openai", "anthropic"],
        help="AI provider (default: auto)",
    )
    parser.add_argument(
        "--write-readme",
        action="store_true",
        help="Write summary to README.md",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Minimal output",
    )
    args = parser.parse_args(argv)

    if args.repo and not _is_valid_repo_spec(args.repo):
        console.print(f"[red]error:[/red] invalid repo: {escape(args.repo)}")
        console.print("  expected: OWNER/REPO (e.g., michaeljabbour/addteam)")
        return 2

    if args.user and args.sync:
        console.print("[red]error:[/red] --sync cannot be used with --user")
        return 2

    # ---------- Check dependencies ----------
    if not shutil.which("gh"):
        console.print("[red]error:[/red] GitHub CLI (gh) not found")
        console.print("  install: https://cli.github.com/")
        return 1

    # ---------- Resolve repo ----------
    view_args = ["repo", "view"]
    if args.repo:
        view_args.append(args.repo)
    view_args.extend(["--json", "name,owner,description"])

    try:
        repo = _gh_json(view_args, what="resolve repo")
    except RuntimeError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 1

    repo_name = repo["name"]
    repo_owner = repo["owner"]["login"]
    description = repo.get("description") or ""

    try:
        me = _gh_text(["api", "user", "--jq", ".login"], what="resolve authenticated user")
    except RuntimeError as exc:
        console.print(f"[red]error:[/red] {exc}")
        return 1

    repo_full_name = f"{repo_owner}/{repo_name}"

    if args.repo:
        first_use_cmd = f"uvx git+https://github.com/michaeljabbour/addteam@main -r {repo_full_name}"
        first_use_note = "Run from any directory."
    else:
        first_use_cmd = "uvx git+https://github.com/michaeljabbour/addteam@main"
        first_use_note = "Run inside the repo you want to manage."

    # ---------- Print header ----------
    if not args.quiet:
        _print_header(repo_name, repo_owner, me, args.dry_run)

    # ---------- Load collaborators ----------
    source_desc: str
    if args.user:
        u = args.user.strip()
        if u.startswith("@"):
            u = u[1:]
        users = [u] if u else []
        source_desc = f"--user {u}"
    else:
        try:
            users, source_desc = _resolve_collaborators(
                args.collaborators_file, repo_owner, repo_name
            )
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            console.print(f"[red]error:[/red] {exc}")
            return 1

    if not users:
        if not args.quiet:
            console.print("  [dim]no collaborators found[/dim]")
        if args.sync:
            console.print("[red]error:[/red] cannot sync with empty list")
            return 2
        return 0

    # ---------- Print config ----------
    if not args.quiet:
        _print_config(source_desc, args.permission, args.sync, len(users))

    # ---------- Add collaborators ----------
    added = 0
    skipped = 0
    failed = 0
    results: list[tuple[str, str, str]] = []  # (user, status, detail)

    for u in users:
        if u == repo_owner:
            results.append((u, "skip", "owner"))
            skipped += 1
            continue
        if u == me:
            results.append((u, "skip", "you"))
            skipped += 1
            continue

        if args.dry_run:
            results.append((u, "would", "invite"))
            added += 1
            continue

        r = _run(
            [
                "gh",
                "api",
                "-X",
                "PUT",
                f"repos/{repo_owner}/{repo_name}/collaborators/{u}",
                "-f",
                f"permission={args.permission}",
            ]
        )

        if r.returncode == 0:
            results.append((u, "ok", "invited"))
            added += 1
        else:
            details = r.stderr.strip() or r.stdout.strip() or "unknown"
            # Truncate long error messages
            if len(details) > 40:
                details = details[:37] + "..."
            results.append((u, "fail", details))
            failed += 1

    # ---------- Print results ----------
    if not args.quiet:
        for user, status, detail in results:
            if status == "ok":
                console.print(f"  [green]✓[/green] {user:<20} [dim]{detail}[/dim]")
            elif status == "would":
                console.print(f"  [blue]○[/blue] {user:<20} [dim]{detail}[/dim]")
            elif status == "skip":
                console.print(f"  [dim]·[/dim] {user:<20} [dim]{detail}[/dim]")
            else:
                console.print(f"  [red]✗[/red] {user:<20} [red]{detail}[/red]")
        console.print()

    # ---------- Sync Mode (Remove extras) ----------
    removed = 0
    if args.sync:
        try:
            current_collabs = _get_collaborators(repo_owner, repo_name)
        except RuntimeError as exc:
            console.print(f"[red]error:[/red] {exc}")
            return 1

        current_collabs.discard(repo_owner)
        current_collabs.discard(me)

        target_users = {u.casefold() for u in users}
        to_remove = sorted(u for u in current_collabs if u.casefold() not in target_users)

        if to_remove:
            if not args.quiet:
                console.print(f"  [yellow]removing {len(to_remove)} unlisted user(s)[/yellow]")
                console.print()

            for u in to_remove:
                if args.dry_run:
                    if not args.quiet:
                        console.print(f"  [blue]○[/blue] {u:<20} [dim]would remove[/dim]")
                    continue

                r = _run(["gh", "api", "-X", "DELETE", f"repos/{repo_owner}/{repo_name}/collaborators/{u}"])

                if r.returncode == 0:
                    if not args.quiet:
                        console.print(f"  [green]✓[/green] {u:<20} [dim]removed[/dim]")
                    removed += 1
                else:
                    if not args.quiet:
                        console.print(f"  [red]✗[/red] {u:<20} [red]remove failed[/red]")

            if not args.quiet:
                console.print()

    # ---------- Summary ----------
    if not args.quiet:
        _print_separator()
        
        parts = []
        if args.dry_run:
            parts.append(f"[blue]{added} would invite[/blue]")
        else:
            if added:
                parts.append(f"[green]{added} invited[/green]")
        if skipped:
            parts.append(f"[dim]{skipped} skipped[/dim]")
        if failed:
            parts.append(f"[red]{failed} failed[/red]")
        if removed:
            parts.append(f"[yellow]{removed} removed[/yellow]")
        
        summary = " · ".join(parts) if parts else "[dim]nothing to do[/dim]"
        console.print(f"  [bold]done[/bold]  {summary}")
        console.print()

    # ---------- Optional AI-generated blurb ----------
    if args.no_ai:
        return 0

    providers_to_try: list[str]
    if args.provider != "auto":
        providers_to_try = [args.provider]
    else:
        providers_to_try = []
        if os.getenv("OPENAI_API_KEY"):
            providers_to_try.append("openai")
        if os.getenv("ANTHROPIC_API_KEY"):
            providers_to_try.append("anthropic")

    if not providers_to_try:
        return 0

    summary: str | None = None
    last_error: Exception | None = None
    used_provider: str | None = None

    for idx, provider in enumerate(providers_to_try):
        if not args.quiet:
            console.print(f"  [dim]generating summary via {provider}...[/dim]")
        try:
            summary = _generate_repo_summary(
                provider=provider,
                repo_full_name=repo_full_name,
                repo_description=description,
                first_use_cmd=first_use_cmd,
            )
            used_provider = provider
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            continue

    if summary is None:
        if not args.quiet and last_error:
            console.print(f"  [yellow]summary generation failed: {last_error}[/yellow]")
        return 0

    summary_out = (
        f"Quick start:\n"
        f"  {first_use_cmd}\n"
        f"  {first_use_note}\n"
        f"  Prereqs: gh installed + authenticated.\n\n"
        f"{summary.strip()}"
    )

    if not args.quiet:
        console.print()
        console.print(Panel(
            summary_out,
            title="[bold]repo summary[/bold]",
            title_align="left",
            border_style="dim",
            padding=(1, 2),
        ))

    if args.write_readme:
        _write_readme_summary(Path("README.md"), summary_out)
        if not args.quiet:
            console.print("  [green]✓[/green] wrote summary to README.md")
            console.print()

    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
