from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

__version__ = "0.2.0"

console = Console()

FALLBACK_COLLABORATORS_REPO = os.getenv("ADDMADETEAM_FALLBACK_COLLABORATORS_REPO", "michaeljabbour/addteam")
VALID_PERMISSIONS = {"pull", "triage", "push", "maintain", "admin"}


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class Collaborator:
    """A collaborator with permission and optional expiry."""
    username: str
    permission: str = "push"
    expires: date | None = None
    from_team: str | None = None  # If resolved from a GitHub team

    @property
    def is_expired(self) -> bool:
        if self.expires is None:
            return False
        return date.today() > self.expires


@dataclass
class TeamConfig:
    """Parsed team configuration from YAML or text file."""
    collaborators: list[Collaborator] = field(default_factory=list)
    default_permission: str = "push"
    welcome_issue: bool = False
    welcome_message: str | None = None
    source: str = ""


@dataclass
class AuditResult:
    """Result of auditing current vs desired state."""
    missing: list[Collaborator] = field(default_factory=list)  # Should have access but don't
    extra: list[str] = field(default_factory=list)  # Have access but shouldn't
    permission_drift: list[tuple[str, str, str]] = field(default_factory=list)  # (user, has, should_have)
    expired: list[Collaborator] = field(default_factory=list)  # Access expired


# =============================================================================
# Shell Helpers
# =============================================================================


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


def _gh_json(args: list[str], *, what: str) -> dict | list:
    result = _run_checked(["gh", *args], what=what)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected non-JSON output while trying to {what}") from exc


def _gh_text(args: list[str], *, what: str) -> str:
    result = _run_checked(["gh", *args], what=what)
    return result.stdout.strip()


# =============================================================================
# File/Path Helpers
# =============================================================================


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
    if len(value) >= 3 and value[1] == ":" and value[2] in ("/", "\\"):
        return True
    return False


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


def _gh_read_repo_file(repo_owner: str, repo_name: str, path: str, *, hostname: str | None = None) -> str:
    cmd = [
        "gh", "api", "-X", "GET", "-H", "Accept: application/vnd.github.raw",
        f"repos/{repo_owner}/{repo_name}/contents/{path}",
    ]
    if hostname:
        cmd[2:2] = ["--hostname", hostname]
    result = _run_checked(cmd, what=f"read {path} from repo")
    return result.stdout


# =============================================================================
# GitHub API Helpers
# =============================================================================


def _get_collaborators_with_permissions(repo_owner: str, repo_name: str) -> dict[str, str]:
    """Fetch collaborators with their permission levels."""
    result = _run_checked(
        [
            "gh", "api", "-X", "GET",
            f"repos/{repo_owner}/{repo_name}/collaborators",
            "--paginate", "-f", "affiliation=direct",
        ],
        what="fetch collaborators",
    )
    collabs = {}
    for item in json.loads(result.stdout) if result.stdout.strip() else []:
        login = item.get("login", "")
        # GitHub returns role_name which maps to permission
        perm = item.get("role_name", "read")
        # Normalize: read->pull, write->push
        if perm == "read":
            perm = "pull"
        elif perm == "write":
            perm = "push"
        if login:
            collabs[login] = perm
    return collabs


def _get_team_members(org: str, team_slug: str) -> list[str]:
    """Fetch members of a GitHub team."""
    try:
        result = _run_checked(
            ["gh", "api", "-X", "GET", f"orgs/{org}/teams/{team_slug}/members", "--paginate", "--jq", ".[].login"],
            what=f"fetch team {org}/{team_slug} members",
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except RuntimeError:
        return []


def _create_welcome_issue(
    repo_owner: str, repo_name: str, username: str, summary: str | None, permission: str
) -> str | None:
    """Create a welcome issue for a new collaborator. Returns issue URL or None."""
    title = f"Welcome @{username}!"
    
    body_parts = [
        f"Hey @{username}, welcome to **{repo_owner}/{repo_name}**! 🎉",
        "",
        f"You've been added as a collaborator with **{permission}** permission.",
        "",
    ]
    
    if summary:
        body_parts.extend([
            "## About this repo",
            "",
            summary,
            "",
        ])
    
    body_parts.extend([
        "## Getting started",
        "",
        "1. Clone the repo: `gh repo clone " + f"{repo_owner}/{repo_name}`",
        "2. Check out the README for setup instructions",
        "3. Feel free to close this issue once you're onboarded!",
        "",
        "---",
        "*This issue was auto-generated by [addteam](https://github.com/michaeljabbour/addteam)*",
    ])
    
    body = "\n".join(body_parts)
    
    try:
        result = _run_checked(
            [
                "gh", "issue", "create",
                "--repo", f"{repo_owner}/{repo_name}",
                "--title", title,
                "--body", body,
                "--assignee", username,
            ],
            what=f"create welcome issue for {username}",
        )
        # gh issue create returns the URL
        return result.stdout.strip()
    except RuntimeError:
        return None


# =============================================================================
# Config Parsing
# =============================================================================


def _parse_usernames_txt(text: str) -> list[str]:
    """Parse simple text file with one username per line."""
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


def _parse_date(value: Any) -> date | None:
    """Parse a date from various formats."""
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        # Try ISO format first
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
        # Try common formats
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    raise ValueError(f"Cannot parse date: {value!r}")


def _parse_yaml_config(content: str, repo_owner: str, repo_name: str) -> TeamConfig:
    """
    Parse YAML team configuration.
    
    Supported formats:
    
    # Simple list (uses default permission)
    collaborators:
      - alice
      - bob
    
    # Role-based groups
    admins:
      - alice
    developers:
      - bob
      - charlie
    reviewers:
      permission: pull
      users:
        - eve
    
    # Full format with expiry
    collaborators:
      - username: contractor
        permission: push
        expires: 2025-03-01
      - username: alice
        permission: admin
    
    # GitHub teams
    teams:
      - org/backend-team
      - org/frontend-team: pull
    
    # Options
    default_permission: push
    welcome_issue: true
    welcome_message: "Custom welcome message"
    """
    data = yaml.safe_load(content)
    if not data:
        return TeamConfig()
    
    if not isinstance(data, dict):
        raise ValueError("YAML must be a dictionary")
    
    config = TeamConfig()
    config.default_permission = data.get("default_permission", "push")
    config.welcome_issue = data.get("welcome_issue", False)
    config.welcome_message = data.get("welcome_message")
    
    # Permission mapping for role names
    role_permissions = {
        "admins": "admin",
        "admin": "admin",
        "maintainers": "maintain",
        "maintainer": "maintain",
        "developers": "push",
        "developer": "push",
        "contributors": "push",
        "contributor": "push",
        "reviewers": "pull",
        "reviewer": "pull",
        "triagers": "triage",
        "triager": "triage",
        "readers": "pull",
        "reader": "pull",
    }
    
    seen_users: set[str] = set()
    
    def add_collaborator(username: str, permission: str, expires: date | None = None, from_team: str | None = None):
        username = username.lstrip("@").strip()
        if not username or username in seen_users:
            return
        seen_users.add(username)
        if permission not in VALID_PERMISSIONS:
            permission = config.default_permission
        config.collaborators.append(Collaborator(
            username=username,
            permission=permission,
            expires=expires,
            from_team=from_team,
        ))
    
    # Parse 'collaborators' key (can be list of strings or list of dicts)
    if "collaborators" in data:
        collabs = data["collaborators"]
        if isinstance(collabs, list):
            for item in collabs:
                if isinstance(item, str):
                    add_collaborator(item, config.default_permission)
                elif isinstance(item, dict):
                    username = item.get("username") or item.get("user") or item.get("name")
                    if username:
                        add_collaborator(
                            username,
                            item.get("permission", config.default_permission),
                            _parse_date(item.get("expires")),
                        )
    
    # Parse role-based groups
    for role_key, permission in role_permissions.items():
        if role_key in data:
            role_data = data[role_key]
            if isinstance(role_data, list):
                for item in role_data:
                    if isinstance(item, str):
                        add_collaborator(item, permission)
                    elif isinstance(item, dict):
                        username = item.get("username") or item.get("user") or item.get("name")
                        if username:
                            add_collaborator(
                                username,
                                item.get("permission", permission),
                                _parse_date(item.get("expires")),
                            )
            elif isinstance(role_data, dict):
                # Format: role: { permission: X, users: [...] }
                actual_perm = role_data.get("permission", permission)
                users = role_data.get("users", [])
                for user in users:
                    if isinstance(user, str):
                        add_collaborator(user, actual_perm)
                    elif isinstance(user, dict):
                        username = user.get("username") or user.get("user") or user.get("name")
                        if username:
                            add_collaborator(
                                username,
                                user.get("permission", actual_perm),
                                _parse_date(user.get("expires")),
                            )
    
    # Parse GitHub teams
    if "teams" in data:
        teams = data["teams"]
        if isinstance(teams, list):
            for team_spec in teams:
                if isinstance(team_spec, str):
                    # Format: org/team-slug
                    if "/" in team_spec:
                        org, team_slug = team_spec.split("/", 1)
                        members = _get_team_members(org, team_slug)
                        for member in members:
                            add_collaborator(member, config.default_permission, from_team=team_spec)
                elif isinstance(team_spec, dict):
                    # Format: { org/team-slug: permission } or { team: org/team-slug, permission: X }
                    for key, value in team_spec.items():
                        if "/" in key:
                            org, team_slug = key.split("/", 1)
                            perm = value if isinstance(value, str) and value in VALID_PERMISSIONS else config.default_permission
                            members = _get_team_members(org, team_slug)
                            for member in members:
                                add_collaborator(member, perm, from_team=key)
    
    return config


def _load_team_config(path: Path, repo_owner: str, repo_name: str) -> TeamConfig:
    """Load team config from file, auto-detecting format."""
    content = path.read_text()
    
    # Detect YAML by extension or content
    is_yaml = (
        path.suffix in (".yaml", ".yml") or
        content.strip().startswith(("{", "[")) is False and
        (":" in content.split("\n")[0] if content.strip() else False)
    )
    
    if is_yaml:
        try:
            return _parse_yaml_config(content, repo_owner, repo_name)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML: {exc}") from exc
    
    # Fall back to simple text format
    users = _parse_usernames_txt(content)
    config = TeamConfig()
    for user in users:
        config.collaborators.append(Collaborator(username=user, permission="push"))
    return config


def _resolve_team_config(
    collab_spec: str, repo_owner: str, repo_name: str
) -> tuple[TeamConfig, str]:
    """
    Resolve team config from the spec.
    Returns (config, source_description).
    """
    repo_full_name = f"{repo_owner}/{repo_name}"
    
    # Try multiple default filenames
    default_files = ["team.yaml", "team.yml", "collaborators.yaml", "collaborators.yml", "collaborators.txt"]
    
    # Explicit repo: prefix
    if collab_spec.startswith("repo:"):
        repo_path = collab_spec.removeprefix("repo:").lstrip("/")
        if not repo_path:
            raise ValueError("repo path is empty")
        content = _gh_read_repo_file(repo_owner, repo_name, repo_path)
        config = _parse_yaml_config(content, repo_owner, repo_name) if repo_path.endswith((".yaml", ".yml")) else TeamConfig(
            collaborators=[Collaborator(u, "push") for u in _parse_usernames_txt(content)]
        )
        config.source = f"{repo_full_name}:{repo_path}"
        return config, config.source

    # Explicit local: prefix
    local_path = collab_spec
    if collab_spec.startswith("local:"):
        local_path = collab_spec.removeprefix("local:")
        if not local_path:
            raise ValueError("local path is empty")
        resolved = _resolve_local_path(local_path, prefer_repo_root=True)
        if not resolved:
            raise FileNotFoundError(f"local file not found: {local_path}")
        config = _load_team_config(resolved, repo_owner, repo_name)
        config.source = f"local:{resolved}"
        return config, config.source

    # Auto-resolve: try local first with multiple filenames
    files_to_try = [collab_spec] if collab_spec not in default_files else []
    files_to_try.extend(default_files)
    
    for filename in files_to_try:
        resolved = _resolve_local_path(filename, prefer_repo_root=True)
        if resolved:
            config = _load_team_config(resolved, repo_owner, repo_name)
            config.source = f"local:{resolved}"
            return config, config.source

    # If it looks like a local path, don't try repo fallback
    if _looks_like_local_path(local_path):
        raise FileNotFoundError(f"local file not found: {local_path}")

    # Try target repo with multiple filenames
    for filename in files_to_try:
        repo_path = filename.lstrip("/")
        try:
            content = _gh_read_repo_file(repo_owner, repo_name, repo_path)
            config = _parse_yaml_config(content, repo_owner, repo_name) if repo_path.endswith((".yaml", ".yml")) else TeamConfig(
                collaborators=[Collaborator(u, "push") for u in _parse_usernames_txt(content)]
            )
            config.source = f"{repo_full_name}:{repo_path}"
            return config, config.source
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            continue

    # Try fallback repo
    fallback_spec = FALLBACK_COLLABORATORS_REPO
    if not _is_valid_repo_spec(fallback_spec) or fallback_spec == repo_full_name:
        raise FileNotFoundError(f"team config not found: {collab_spec}")

    host, fallback_owner, fallback_repo = _split_repo_spec(fallback_spec)
    
    for filename in files_to_try:
        repo_path = filename.lstrip("/")
        try:
            content = _gh_read_repo_file(fallback_owner, fallback_repo, repo_path, hostname=host)
            config = _parse_yaml_config(content, repo_owner, repo_name) if repo_path.endswith((".yaml", ".yml")) else TeamConfig(
                collaborators=[Collaborator(u, "push") for u in _parse_usernames_txt(content)]
            )
            config.source = f"{fallback_owner}/{fallback_repo}:{repo_path} (fallback)"
            return config, config.source
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
            continue
    
    raise FileNotFoundError(f"team config not found: {collab_spec}")


# =============================================================================
# Audit
# =============================================================================


def _audit_collaborators(
    config: TeamConfig, repo_owner: str, repo_name: str, me: str
) -> AuditResult:
    """Compare desired state (config) with actual state (GitHub)."""
    result = AuditResult()
    
    # Get current collaborators with their permissions
    current = _get_collaborators_with_permissions(repo_owner, repo_name)
    
    # Build desired state map (excluding owner and self)
    desired: dict[str, Collaborator] = {}
    for collab in config.collaborators:
        if collab.username == repo_owner or collab.username == me:
            continue
        if collab.is_expired:
            result.expired.append(collab)
        else:
            desired[collab.username.casefold()] = collab
    
    # Find missing (in desired but not in current)
    for username_lower, collab in desired.items():
        found = False
        for current_user in current:
            if current_user.casefold() == username_lower:
                found = True
                # Check permission drift
                current_perm = current[current_user]
                if current_perm != collab.permission:
                    result.permission_drift.append((current_user, current_perm, collab.permission))
                break
        if not found:
            result.missing.append(collab)
    
    # Find extra (in current but not in desired)
    for current_user in current:
        if current_user == repo_owner or current_user == me:
            continue
        if current_user.casefold() not in desired:
            result.extra.append(current_user)
    
    return result


# =============================================================================
# HTTP/AI Helpers
# =============================================================================


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
    prompt = "\n".join([
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
    ])

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
                "max_tokens": 200,
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
                "max_tokens": 200,
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

    existing = readme_path.read_text() if readme_path.exists() else ""
    block = f"{begin}\n\n{summary.strip()}\n\n{end}\n"

    if begin in existing and end in existing:
        before = existing.split(begin, 1)[0]
        after = existing.split(end, 1)[1]
        readme_path.write_text(before + block + after.lstrip("\n"))
    elif existing.strip():
        readme_path.write_text(existing.rstrip() + "\n\n" + block)
    else:
        readme_path.write_text(block)


# =============================================================================
# Output Helpers
# =============================================================================


def _print_header(repo_name: str, repo_owner: str, me: str, mode: str | None = None) -> None:
    title = Text()
    title.append("addteam", style="bold magenta")
    title.append(f" v{__version__}", style="dim")
    if mode:
        title.append(f"  [{mode}]", style="bold yellow")
    
    console.print()
    console.print(title)
    console.print()
    console.print(f"  [bold]{repo_name}[/bold] [dim]({repo_owner})[/dim]")
    console.print(f"  [dim]authenticated as[/dim] {me}")
    console.print()


def _print_config(source: str, default_perm: str, sync: bool, user_count: int, welcome: bool = False) -> None:
    console.print(f"  [dim]source[/dim]      {source}")
    console.print(f"  [dim]permission[/dim]  {default_perm}")
    if sync:
        console.print("  [dim]mode[/dim]        sync (will remove unlisted)")
    if welcome:
        console.print("  [dim]welcome[/dim]     create issues for new users")
    console.print(f"  [dim]users[/dim]       {user_count}")
    console.print()


def _print_separator() -> None:
    console.print("  " + "─" * 50, style="dim")
    console.print()


def _normalize_argv(argv: list[str]) -> list[str]:
    normalized: list[str] = []
    for arg in argv:
        for prefix in ("--repo", "--provider", "--permission", "--file"):
            if arg.startswith(prefix) and arg != prefix and not arg.startswith(f"{prefix}="):
                value = arg[len(prefix):]
                if value:
                    normalized.extend([prefix, value])
                    break
        else:
            normalized.append(arg)
    return normalized


# =============================================================================
# Main Entry Point
# =============================================================================


def run(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = _normalize_argv(argv)

    parser = argparse.ArgumentParser(
        prog="addteam",
        description="Collaborator management for GitHub repos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  addteam                     # run in current repo
  addteam -r owner/repo       # target specific repo
  addteam -u octocat          # invite single user
  addteam -n                  # dry-run (preview)
  addteam -s                  # sync mode (remove unlisted)
  addteam -f team.yaml        # use custom file
  addteam --audit             # show drift without changes
  addteam --welcome           # create welcome issues
""",
    )
    parser.add_argument("-f", "--file", default="team.yaml", metavar="FILE",
                        help="Team config file (default: team.yaml, falls back to collaborators.txt)")
    parser.add_argument("-u", "--user", metavar="NAME", help="Invite a single GitHub user")
    parser.add_argument("-p", "--permission", default="push", choices=list(VALID_PERMISSIONS),
                        help="Permission level (default: push)")
    parser.add_argument("-r", "--repo", metavar="OWNER/REPO", help="Target repo (default: current directory)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("-s", "--sync", action="store_true", help="Remove collaborators not in the list")
    parser.add_argument("-a", "--audit", action="store_true", help="Show drift without making changes")
    parser.add_argument("-w", "--welcome", action="store_true", help="Create welcome issues for new collaborators")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI-generated summary")
    parser.add_argument("--provider", default="auto", choices=["auto", "openai", "anthropic"],
                        help="AI provider (default: auto)")
    parser.add_argument("--write-readme", action="store_true", help="Write summary to README.md")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    args = parser.parse_args(argv)

    # Validation
    if args.repo and not _is_valid_repo_spec(args.repo):
        console.print(f"[red]error:[/red] invalid repo: {escape(args.repo)}")
        return 2

    if args.user and args.sync:
        console.print("[red]error:[/red] --sync cannot be used with --user")
        return 2

    if not shutil.which("gh"):
        console.print("[red]error:[/red] GitHub CLI (gh) not found")
        console.print("  install: https://cli.github.com/")
        return 1

    # Resolve repo
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
    
    # Determine mode label
    mode = None
    if args.dry_run:
        mode = "dry-run"
    elif args.audit:
        mode = "audit"

    if not args.quiet:
        _print_header(repo_name, repo_owner, me, mode)

    # Build first_use command for AI summary
    if args.repo:
        first_use_cmd = f"uvx git+https://github.com/michaeljabbour/addteam@main -r {repo_full_name}"
        first_use_note = "Run from any directory."
    else:
        first_use_cmd = "uvx git+https://github.com/michaeljabbour/addteam@main"
        first_use_note = "Run inside the repo you want to manage."

    # Load config
    if args.user:
        u = args.user.lstrip("@").strip()
        config = TeamConfig(
            collaborators=[Collaborator(u, args.permission)] if u else [],
            source=f"--user {u}",
        )
    else:
        try:
            config, _ = _resolve_team_config(args.file, repo_owner, repo_name)
        except (ValueError, FileNotFoundError, RuntimeError) as exc:
            console.print(f"[red]error:[/red] {exc}")
            return 1

    # Apply CLI overrides
    if args.welcome:
        config.welcome_issue = True

    if not config.collaborators:
        if not args.quiet:
            console.print("  [dim]no collaborators found[/dim]")
        if args.sync:
            console.print("[red]error:[/red] cannot sync with empty list")
            return 2
        return 0

    if not args.quiet:
        _print_config(config.source, args.permission, args.sync, len(config.collaborators), config.welcome_issue)

    # ==========================================================================
    # AUDIT MODE
    # ==========================================================================
    if args.audit:
        audit = _audit_collaborators(config, repo_owner, repo_name, me)
        
        has_drift = bool(audit.missing or audit.extra or audit.permission_drift or audit.expired)
        
        if not has_drift:
            console.print("  [green]✓ no drift detected[/green]")
            console.print()
            return 0
        
        console.print("  [yellow]⚠ drift detected[/yellow]")
        console.print()
        
        if audit.missing:
            console.print("  [bold]Missing[/bold] (should have access):")
            for c in audit.missing:
                team_note = f" [dim]from {c.from_team}[/dim]" if c.from_team else ""
                console.print(f"    [green]+[/green] {c.username} ({c.permission}){team_note}")
            console.print()
        
        if audit.extra:
            console.print("  [bold]Extra[/bold] (should not have access):")
            for u in audit.extra:
                console.print(f"    [red]-[/red] {u}")
            console.print()
        
        if audit.permission_drift:
            console.print("  [bold]Permission drift[/bold]:")
            for user, has, should in audit.permission_drift:
                console.print(f"    [yellow]~[/yellow] {user}: {has} → {should}")
            console.print()
        
        if audit.expired:
            console.print("  [bold]Expired[/bold] (should be removed):")
            for c in audit.expired:
                console.print(f"    [red]⏰[/red] {c.username} (expired {c.expires})")
            console.print()
        
        _print_separator()
        total = len(audit.missing) + len(audit.extra) + len(audit.permission_drift) + len(audit.expired)
        console.print(f"  [bold]total drift:[/bold] {total} item(s)")
        console.print()
        console.print("  [dim]run without --audit to apply changes[/dim]")
        console.print()
        return 0

    # ==========================================================================
    # APPLY MODE
    # ==========================================================================
    added = 0
    skipped = 0
    failed = 0
    removed = 0
    welcomed = 0
    results: list[tuple[str, str, str]] = []

    # Generate AI summary upfront if we'll need it for welcome issues
    ai_summary: str | None = None
    if config.welcome_issue and not args.no_ai:
        providers_to_try = []
        if args.provider != "auto":
            providers_to_try = [args.provider]
        else:
            if os.getenv("OPENAI_API_KEY"):
                providers_to_try.append("openai")
            if os.getenv("ANTHROPIC_API_KEY"):
                providers_to_try.append("anthropic")
        
        for provider in providers_to_try:
            try:
                ai_summary = _generate_repo_summary(
                    provider=provider,
                    repo_full_name=repo_full_name,
                    repo_description=description,
                    first_use_cmd=first_use_cmd,
                )
                break
            except Exception:
                continue

    # Process collaborators
    for collab in config.collaborators:
        u = collab.username
        
        if u == repo_owner:
            results.append((u, "skip", "owner"))
            skipped += 1
            continue
        if u == me:
            results.append((u, "skip", "you"))
            skipped += 1
            continue
        if collab.is_expired:
            results.append((u, "skip", f"expired {collab.expires}"))
            skipped += 1
            continue

        if args.dry_run:
            team_note = f" ({collab.from_team})" if collab.from_team else ""
            results.append((u, "would", f"invite [{collab.permission}]{team_note}"))
            added += 1
            continue

        r = _run([
            "gh", "api", "-X", "PUT",
            f"repos/{repo_owner}/{repo_name}/collaborators/{u}",
            "-f", f"permission={collab.permission}",
        ])

        if r.returncode == 0:
            team_note = f" ({collab.from_team})" if collab.from_team else ""
            results.append((u, "ok", f"invited [{collab.permission}]{team_note}"))
            added += 1
            
            # Create welcome issue if enabled
            if config.welcome_issue:
                issue_url = _create_welcome_issue(
                    repo_owner, repo_name, u,
                    config.welcome_message or ai_summary,
                    collab.permission,
                )
                if issue_url:
                    welcomed += 1
        else:
            details = r.stderr.strip() or r.stdout.strip() or "unknown"
            if len(details) > 40:
                details = details[:37] + "..."
            results.append((u, "fail", details))
            failed += 1

    # Print results
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

    # Sync mode: remove extras and expired
    if args.sync:
        try:
            current_collabs = set(_get_collaborators_with_permissions(repo_owner, repo_name).keys())
        except RuntimeError as exc:
            console.print(f"[red]error:[/red] {exc}")
            return 1

        current_collabs.discard(repo_owner)
        current_collabs.discard(me)

        # Build set of valid (non-expired) usernames
        valid_users = {c.username.casefold() for c in config.collaborators if not c.is_expired}
        to_remove = sorted(u for u in current_collabs if u.casefold() not in valid_users)
        
        # Also remove expired users
        expired_users = [c.username for c in config.collaborators if c.is_expired]
        for eu in expired_users:
            if eu.casefold() in {u.casefold() for u in current_collabs} and eu not in to_remove:
                to_remove.append(eu)

        if to_remove:
            if not args.quiet:
                console.print(f"  [yellow]removing {len(to_remove)} user(s)[/yellow]")
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

    # Summary
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
        if welcomed:
            parts.append(f"[cyan]{welcomed} welcomed[/cyan]")
        
        summary = " · ".join(parts) if parts else "[dim]nothing to do[/dim]"
        console.print(f"  [bold]done[/bold]  {summary}")
        console.print()

    # AI summary (for display, not for welcome issues which already got it)
    if args.no_ai:
        return 0

    providers_to_try = []
    if args.provider != "auto":
        providers_to_try = [args.provider]
    else:
        if os.getenv("OPENAI_API_KEY"):
            providers_to_try.append("openai")
        if os.getenv("ANTHROPIC_API_KEY"):
            providers_to_try.append("anthropic")

    if not providers_to_try:
        return 0

    # Use cached summary if we already generated it
    summary = ai_summary
    if not summary:
        for provider in providers_to_try:
            if not args.quiet:
                console.print(f"  [dim]generating summary via {provider}...[/dim]")
            try:
                summary = _generate_repo_summary(
                    provider=provider,
                    repo_full_name=repo_full_name,
                    repo_description=description,
                    first_use_cmd=first_use_cmd,
                )
                break
            except Exception as exc:
                if not args.quiet:
                    console.print(f"  [yellow]failed: {exc}[/yellow]")
                continue

    if not summary:
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
