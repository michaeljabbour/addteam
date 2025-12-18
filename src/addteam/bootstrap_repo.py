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

console = Console()


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


def _load_usernames(path: Path) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"{path.as_posix()} not found")
    return _parse_usernames(path.read_text())


def _gh_read_repo_file(repo_owner: str, repo_name: str, path: str) -> str:
    result = _run_checked(
        [
            "gh",
            "api",
            "-X",
            "GET",
            "-H",
            "Accept: application/vnd.github.raw",
            f"repos/{repo_owner}/{repo_name}/contents/{path}",
        ],
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
    *, provider: str, repo_full_name: str, repo_description: str, timeout_s: int = 30
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
            "- the fastest way to get started",
            "Keep it crisp and practical.",
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


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bootstrap repo collaborators + optional AI summary.")
    parser.add_argument(
        "--collaborators-file",
        default="collaborators.txt",
        help="Path to a newline-delimited list of GitHub usernames (local path or repo path).",
    )
    parser.add_argument(
        "--user",
        help="Invite exactly one GitHub username (skips collaborators file).",
    )
    parser.add_argument(
        "--permission",
        default="push",
        choices=["pull", "triage", "push", "maintain", "admin"],
        help="Permission to grant collaborators.",
    )
    parser.add_argument(
        "--repo",
        help="Repo to target as OWNER/NAME (defaults to the current directory's repo).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying GitHub.")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Remove collaborators who are not in the list (requires admin).",
    )
    parser.add_argument("--no-ai", action="store_true", help="Skip AI-generated repo summary.")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "openai", "anthropic"],
        help="AI provider to use (default: auto-detect via env vars).",
    )
    parser.add_argument(
        "--write-readme",
        action="store_true",
        help="Write the generated summary into README.md between markers.",
    )
    args = parser.parse_args(argv)

    if args.user and args.sync:
        console.print("[bold red]❌ Error:[/bold red] `--sync` cannot be used with `--user`.")
        console.print("   `--sync` enforces the full desired list from `collaborators.txt`.")
        return 2

    # ---------- Check dependencies ----------
    if not shutil.which("gh"):
        console.print("[bold red]❌ Error:[/bold red] GitHub CLI (`gh`) is not installed or not in PATH.")
        console.print("   Please install it: https://cli.github.com/")
        return 1

    # ---------- Resolve repo ----------
    console.print("[bold blue]🔍 Resolving repo...[/bold blue]")
    view_args = ["repo", "view"]
    if args.repo:
        view_args.append(args.repo)
    view_args.extend(["--json", "name,owner,description"])

    try:
        repo = _gh_json(view_args, what="resolve repo")
    except RuntimeError as exc:
        console.print(f"[bold red]❌ Failed to resolve repo:[/bold red] {exc}")
        return 1

    repo_name = repo["name"]
    repo_owner = repo["owner"]["login"]
    description = repo.get("description") or ""

    try:
        me = _gh_text(["api", "user", "--jq", ".login"], what="resolve authenticated user")
    except RuntimeError as exc:
        console.print(f"[bold red]❌ Failed to get current user:[/bold red] {exc}")
        return 1

    repo_full_name = f"{repo_owner}/{repo_name}"
    console.print(f"📦 Repo: [bold]{repo_full_name}[/bold]")
    console.print(f"👑 Repo owner: [bold]{repo_owner}[/bold]")
    console.print(f"👤 Auth user: [bold]{me}[/bold]")
    console.print()

    # ---------- Load collaborators ----------
    if args.user:
        u = args.user.strip()
        if u.startswith("@"):
            u = u[1:]
        users = [u] if u else []
    else:
        collab_path = Path(args.collaborators_file)
        try:
            users = _load_usernames(collab_path)
        except RuntimeError:
            if collab_path.is_absolute():
                console.print(f"[bold red]❌ collaborators file not found:[/bold red] {collab_path.as_posix()}")
                return 1
            console.print(f"ℹ️  {args.collaborators_file} not found locally; fetching from {repo_full_name}...")
            try:
                users = _parse_usernames(_gh_read_repo_file(repo_owner, repo_name, args.collaborators_file))
            except RuntimeError as exc:
                console.print(f"[bold red]❌ Failed to load collaborators list:[/bold red] {exc}")
                return 1

    if not users:
        console.print("ℹ️  No collaborators found; nothing to do.")
        if args.sync:
            console.print("[bold red]❌ Refusing to `--sync` with an empty list.[/bold red]")
            console.print("   Add at least one username to the file, or omit `--sync`.")
            return 2
        return 0

    # ---------- Add collaborators ----------
    added = 0
    skipped = 0
    failed = 0
    would_add = 0

    with console.status("[bold green]Processing collaborators...[/bold green]"):
        for u in users:
            if u == repo_owner:
                console.print(f"⏭️  Skipping [bold]{u}[/bold] (repo owner)")
                skipped += 1
                continue
            if u == me:
                console.print(f"⏭️  Skipping [bold]{u}[/bold] (you)")
                skipped += 1
                continue

            msg = f"➕ Adding [bold]{u}[/bold] … "
            if args.dry_run:
                console.print(f"{msg}[blue]dry-run[/blue]")
                would_add += 1
                continue

            console.print(msg, end="")
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
                console.print("[green]invited / updated[/green]")
                added += 1
            else:
                console.print("[red]failed[/red]")
                details = r.stderr.strip() or r.stdout.strip() or "unknown error"
                console.print(f"   [red]{escape(details)}[/red]")
                failed += 1

    # ---------- Sync Mode (Remove extras) ----------
    if args.sync:
        console.print("\n[bold blue]🔄 Sync mode: Checking for collaborators to remove...[/bold blue]")
        try:
            current_collabs = _get_collaborators(repo_owner, repo_name)
        except RuntimeError as exc:
            console.print(f"[bold red]❌ Failed to fetch current collaborators:[/bold red] {exc}")
            return 1

        # Don't remove owner or yourself
        current_collabs.discard(repo_owner)
        current_collabs.discard(me)

        target_users = {u.casefold() for u in users}
        to_remove = sorted(u for u in current_collabs if u.casefold() not in target_users)

        if to_remove:
            console.print(
                f"[bold yellow]⚠️  Found {len(to_remove)} user(s) not in the list:[/bold yellow] {', '.join(to_remove)}"
            )

            for u in to_remove:
                msg = f"➖ Removing [bold]{u}[/bold] … "
                if args.dry_run:
                    console.print(f"{msg}[blue]dry-run[/blue]")
                    continue

                console.print(msg, end="")
                r = _run(["gh", "api", "-X", "DELETE", f"repos/{repo_owner}/{repo_name}/collaborators/{u}"])

                if r.returncode == 0:
                    console.print("[green]removed[/green]")
                else:
                    console.print("[red]failed[/red]")
                    details = r.stderr.strip() or r.stdout.strip() or "unknown error"
                    console.print(f"   [red]{escape(details)}[/red]")

        else:
            console.print("[green]✅ No extra collaborators found.[/green]")

    # ---------- Summary ----------
    if args.dry_run:
        console.print(f"\n✅ Done (dry-run). would_add={would_add} skipped={skipped}")
    else:
        console.print(f"\n✅ Done. added={added} skipped={skipped} failed={failed}")

    # ---------- Optional AI-generated blurb ----------
    if args.no_ai:
        return 0

    provider = args.provider
    if provider == "auto":
        provider = "openai" if os.getenv("OPENAI_API_KEY") else "anthropic" if os.getenv("ANTHROPIC_API_KEY") else ""

    console.print("\n[bold magenta]🧠 Checking for local AI keys...[/bold magenta]")
    if not provider:
        console.print("ℹ️  No OPENAI_API_KEY or ANTHROPIC_API_KEY found. Skipping summary.")
        return 0

    console.print(f"✍️  Generating short repo summary via [bold]{provider}[/bold]...")
    try:
        summary = _generate_repo_summary(
            provider=provider,
            repo_full_name=repo_full_name,
            repo_description=description,
        )
    except Exception as exc:
        console.print(f"[bold red]❌ Failed to generate summary:[/bold red] {exc}")
        return 0

    console.print("\n[bold]📣 Repo summary:[/bold]\n")
    console.print(summary)

    if args.write_readme:
        _write_readme_summary(Path("README.md"), summary)
        console.print("\n[green]📝 Wrote summary into README.md[/green]")

    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

