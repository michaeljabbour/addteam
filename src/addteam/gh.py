"""GitHub interactions — all GitHub operations go through the `gh` CLI.

Fetch-side helpers live here. Mutations (invite/remove) stay in app.py so
handlers can report each result as it happens.
"""

from __future__ import annotations

import json
import subprocess

from .console import warning
from .models import GITHUB_PERMISSION_MAP


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _run_checked(cmd: list[str], *, what: str) -> subprocess.CompletedProcess[str]:
    try:
        result = _run(cmd)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing dependency for {what}: {cmd[0]!r} not found") from exc

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"Failed to {what}: {details}")
    return result


def _gh_json(args: list[str], *, what: str) -> dict | list:
    result = _run_checked(["gh", *args], what=what)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected non-JSON output while trying to {what}") from exc


def _gh_text(args: list[str], *, what: str) -> str:
    result = _run_checked(["gh", *args], what=what)
    return result.stdout.strip()


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
    result = _run_checked(cmd, what=f"read {path} from repo")
    return result.stdout


def _gh_api_paginated(args: list[str], *, what: str) -> list[dict]:
    """Run a paginating `gh api` list call and return the flattened items.

    Uses --slurp so multi-page results arrive as one JSON array-of-arrays;
    without it gh concatenates raw JSON documents per page and parsing fails
    on any repo with more than one page of results.
    """
    result = _run_checked(["gh", "api", "-X", "GET", *args, "--paginate", "--slurp"], what=what)
    raw = result.stdout.strip()
    if not raw:
        return []
    try:
        pages = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected non-JSON output while trying to {what}") from exc
    if not isinstance(pages, list):
        return []
    # --slurp yields a list of pages (each a list of items). Be lenient and
    # also accept a plain list of items so mocks/tests stay simple.
    items: list[dict] = []
    for entry in pages:
        if isinstance(entry, list):
            items.extend(item for item in entry if isinstance(item, dict))
        elif isinstance(entry, dict):
            items.append(entry)
    return items


def _get_collaborators_with_permissions(repo_owner: str, repo_name: str) -> dict[str, str]:
    """Fetch collaborators who have accepted (have access)."""
    items = _gh_api_paginated(
        [f"repos/{repo_owner}/{repo_name}/collaborators", "-f", "affiliation=direct"],
        what="fetch collaborators",
    )
    collabs: dict[str, str] = {}
    for item in items:
        login = item.get("login", "")
        perm = item.get("role_name") or "read"
        perm = GITHUB_PERMISSION_MAP.get(perm, perm)
        if login:
            collabs[login] = perm
    return collabs


def _get_pending_invitations(repo_owner: str, repo_name: str) -> set[str]:
    """Fetch usernames with pending invitations (not yet accepted)."""
    try:
        items = _gh_api_paginated(
            [f"repos/{repo_owner}/{repo_name}/invitations"],
            what="fetch pending invitations",
        )
        pending = set()
        for item in items:
            invitee = item.get("invitee") or {}
            login = invitee.get("login", "")
            if login:
                pending.add(login)
        return pending
    except RuntimeError as exc:
        warning(f"could not fetch pending invitations (you may lack admin rights): {exc}")
        return set()


def _get_team_members(org: str, team_slug: str) -> list[str]:
    """Fetch members of a GitHub team.

    Raises RuntimeError on failure — callers must not treat "unknown" as
    "empty", or an API blip could strip a whole team's access during --sync.
    """
    try:
        result = _run_checked(
            ["gh", "api", "-X", "GET", f"orgs/{org}/teams/{team_slug}/members", "--paginate", "--jq", ".[].login"],
            what=f"fetch team {org}/{team_slug} members",
        )
    except RuntimeError as exc:
        raise RuntimeError(f"could not fetch team {org}/{team_slug}: {exc}") from exc
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _get_repo_info(repo_owner: str, repo_name: str) -> dict:
    """Fetch detailed repo info for welcome message."""
    try:
        result = _run_checked(
            [
                "gh",
                "api",
                f"repos/{repo_owner}/{repo_name}",
                "--jq",
                "{description,homepage,language,default_branch,html_url,topics}",
            ],
            what="fetch repo info",
        )
        return json.loads(result.stdout)
    except (RuntimeError, json.JSONDecodeError):
        return {}


def _get_readme_excerpt(repo_owner: str, repo_name: str, max_lines: int = 30) -> str | None:
    """Fetch first section of README for context."""
    try:
        result = _run_checked(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw", f"repos/{repo_owner}/{repo_name}/readme"],
            what="fetch README",
        )
        lines = result.stdout.strip().split("\n")[:max_lines]
        return "\n".join(lines)
    except RuntimeError:
        return None


def _create_welcome_issue(
    repo_owner: str, repo_name: str, username: str, summary: str | None, permission: str
) -> str | None:
    """Create a welcome issue for a new collaborator."""
    title = f"Welcome @{username}!"
    repo_full = f"{repo_owner}/{repo_name}"

    info = _get_repo_info(repo_owner, repo_name)
    description = info.get("description") or ""
    homepage = info.get("homepage") or ""
    language = info.get("language") or ""
    html_url = info.get("html_url") or f"https://github.com/{repo_full}"
    topics = info.get("topics") or []

    body_parts = [
        f"Hey @{username}, welcome to **{repo_full}**! 🎉",
        "",
        f"You've been added as a collaborator with **{permission}** permission.",
        "",
    ]

    if summary:
        body_parts.extend(["## About this repo", "", summary, ""])
    elif description:
        body_parts.extend(["## About this repo", "", description, ""])

    if topics:
        body_parts.extend([f"**Topics:** {', '.join(topics)}", ""])

    body_parts.extend(
        [
            "## Getting started",
            "",
            "```bash",
            "# Clone the repo",
            f"gh repo clone {repo_full}",
            f"cd {repo_name}",
            "",
            "# Check out the README",
            "cat README.md",
            "```",
            "",
        ]
    )

    if language:
        hints = {
            "Python": "# Install dependencies\npip install -e . # or: uv sync",
            "JavaScript": "# Install dependencies\nnpm install",
            "TypeScript": "# Install dependencies\nnpm install",
            "Rust": "# Build\ncargo build",
            "Go": "# Build\ngo build",
        }
        if language in hints:
            body_parts.extend(["```bash", hints[language], "```", ""])

    body_parts.extend(["## Links", "", f"- 📖 [README]({html_url}#readme)"])
    if homepage:
        body_parts.append(f"- 🌐 [Homepage]({homepage})")
    body_parts.extend(
        [
            "",
            "---",
            "*This issue was auto-generated by [addteam](https://github.com/michaeljabbour/addteam)*",
        ]
    )

    body = "\n".join(body_parts)

    try:
        result = _run_checked(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo_full,
                "--title",
                title,
                "--body",
                body,
                "--assignee",
                username,
            ],
            what=f"create welcome issue for {username}",
        )
        return result.stdout.strip()
    except RuntimeError:
        return None
