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
from rich.text import Text

__version__ = "0.8.6"

console = Console()


def _check_for_updates() -> None:
    """Check PyPI for newer version and notify user."""
    try:
        resp = httpx.get("https://pypi.org/pypi/addteam/json", timeout=2)
        if resp.status_code != 200:
            return
        latest = resp.json().get("info", {}).get("version", "")
        if not latest:
            return
        # Simple version comparison (works for semver without pre-releases)
        current_parts = [int(x) for x in __version__.split(".")]
        latest_parts = [int(x) for x in latest.split(".")]
        if latest_parts > current_parts:
            console.print(f"  [dim]update available: {__version__} → {latest}  (uvx --refresh addteam | pip install -U addteam)[/dim]")
            console.print()
    except Exception:
        pass  # Fail silently - don't interrupt the user

VALID_PERMISSIONS = {"pull", "triage", "push", "maintain", "admin"}

# =============================================================================
# Init Templates
# =============================================================================

TEAM_YAML_TEMPLATE = """\
# Team configuration for {repo_name}
# Docs: https://github.com/michaeljabbour/addteam

default_permission: push

# Role-based groups (permission inferred from role name)
admins:
  - {owner}

developers:
  # - alice
  # - bob

# reviewers:
#   - eve

# Temporary access with expiry dates
# contractors:
#   - username: temp-dev
#     permission: push
#     expires: 2025-06-01

# GitHub team integration (for orgs)
# teams:
#   - myorg/backend-team
#   - myorg/frontend-team: pull

# Auto-create welcome issues for new collaborators
# welcome_issue: true
"""

GITHUB_ACTION_TEMPLATE = """\
# Sync collaborators on push to team.yaml
# This workflow enforces team.yaml as the source of truth for repo access

name: Sync Collaborators

on:
  push:
    branches: [main]
    paths:
      - 'team.yaml'
  workflow_dispatch:  # Allow manual trigger

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write  # For welcome issues (optional)
    
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install addteam
        run: pip install addteam
      
      - name: Sync collaborators
        env:
          GH_TOKEN: ${{{{ secrets.TEAM_SYNC_TOKEN }}}}
          # Optional: for AI-generated welcome messages
          # OPENAI_API_KEY: ${{{{ secrets.OPENAI_API_KEY }}}}
        run: |
          addteam --sync --no-ai
"""

GITHUB_ACTION_MULTI_REPO_TEMPLATE = """\
# Sync collaborators across multiple repos
# This workflow uses this repo as the source of truth for team membership

name: Sync Team Across Repos

on:
  push:
    branches: [main]
    paths:
      - 'team.yaml'
      - 'repos.txt'
  workflow_dispatch:
  schedule:
    - cron: '0 9 * * 1'  # Weekly on Monday 9am UTC

jobs:
  sync:
    runs-on: ubuntu-latest
    
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      
      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      
      - name: Install addteam
        run: pip install addteam
      
      - name: Sync all repos
        env:
          GH_TOKEN: ${{{{ secrets.TEAM_SYNC_TOKEN }}}}
        run: |
          # Read repos from repos.txt (one per line)
          while IFS= read -r repo || [[ -n "$repo" ]]; do
            [[ "$repo" =~ ^#.*$ || -z "$repo" ]] && continue
            echo "::group::Syncing $repo"
            addteam -r "$repo" -f team.yaml --sync --no-ai || echo "Failed: $repo"
            echo "::endgroup::"
          done < repos.txt
"""

REPOS_TXT_TEMPLATE = """\
# Repos to sync with team.yaml (one per line)
# Lines starting with # are ignored

# Example:
# myorg/repo1
# myorg/repo2
# myorg/repo3
"""


# =============================================================================
# Data Models
# =============================================================================


@dataclass
class Collaborator:
    """A collaborator with permission and optional expiry."""
    username: str
    permission: str = "push"
    expires: date | None = None
    from_team: str | None = None

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
    missing: list[Collaborator] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    permission_drift: list[tuple[str, str, str]] = field(default_factory=list)
    expired: list[Collaborator] = field(default_factory=list)


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
    """Fetch collaborators who have accepted (have access)."""
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
        perm = item.get("role_name", "read")
        if perm == "read":
            perm = "pull"
        elif perm == "write":
            perm = "push"
        if login:
            collabs[login] = perm
    return collabs


def _get_pending_invitations(repo_owner: str, repo_name: str) -> set[str]:
    """Fetch usernames with pending invitations (not yet accepted)."""
    try:
        result = _run_checked(
            [
                "gh", "api", "-X", "GET",
                f"repos/{repo_owner}/{repo_name}/invitations",
                "--paginate",
            ],
            what="fetch pending invitations",
        )
        pending = set()
        for item in json.loads(result.stdout) if result.stdout.strip() else []:
            invitee = item.get("invitee", {})
            login = invitee.get("login", "") if invitee else ""
            if login:
                pending.add(login)
        return pending
    except RuntimeError as exc:
        console.print(f"  [yellow]warning:[/yellow] could not fetch pending invitations (you may lack admin rights): {exc}")
        return set()


def _get_team_members(org: str, team_slug: str) -> list[str]:
    """Fetch members of a GitHub team."""
    try:
        result = _run_checked(
            ["gh", "api", "-X", "GET", f"orgs/{org}/teams/{team_slug}/members", "--paginate", "--jq", ".[].login"],
            what=f"fetch team {org}/{team_slug} members",
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except RuntimeError as exc:
        console.print(f"  [yellow]warning:[/yellow] could not fetch team {org}/{team_slug}: {exc}")
        return []


def _get_repo_info(repo_owner: str, repo_name: str) -> dict:
    """Fetch detailed repo info for welcome message."""
    try:
        result = _run_checked(
            ["gh", "api", f"repos/{repo_owner}/{repo_name}",
             "--jq", "{description,homepage,language,default_branch,html_url,topics}"],
            what="fetch repo info",
        )
        return json.loads(result.stdout)
    except (RuntimeError, json.JSONDecodeError):
        return {}


def _get_readme_excerpt(repo_owner: str, repo_name: str, max_lines: int = 30) -> str | None:
    """Fetch first section of README for context."""
    try:
        result = _run_checked(
            ["gh", "api", "-H", "Accept: application/vnd.github.raw",
             f"repos/{repo_owner}/{repo_name}/readme"],
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
    
    # Fetch repo details
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
    
    # Add AI summary or description
    if summary:
        body_parts.extend(["## About this repo", "", summary, ""])
    elif description:
        body_parts.extend(["## About this repo", "", description, ""])
    
    # Add topics if available
    if topics:
        body_parts.extend([f"**Topics:** {', '.join(topics)}", ""])
    
    # Getting started section
    body_parts.extend([
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
    ])
    
    # Add language-specific hints
    if language:
        hints = {
            "Python": "# Install dependencies\npip install -e . # or: uv sync",
            "JavaScript": "# Install dependencies\nnpm install",
            "TypeScript": "# Install dependencies\nnpm install",
            "Rust": "# Build\ncargo build",
            "Go": "# Build\ngo build",
        }
        if language in hints:
            body_parts.extend([
                "```bash",
                hints[language],
                "```",
                "",
            ])
    
    # Links
    body_parts.extend([
        "## Links",
        "",
        f"- 📖 [README]({html_url}#readme)",
    ])
    if homepage:
        body_parts.append(f"- 🌐 [Homepage]({homepage})")
    body_parts.extend([
        "",
        "---",
        "*This issue was auto-generated by [addteam](https://github.com/michaeljabbour/addteam)*",
    ])
    
    body = "\n".join(body_parts)
    
    try:
        result = _run_checked(
            [
                "gh", "issue", "create",
                "--repo", repo_full,
                "--title", title,
                "--body", body,
                "--assignee", username,
            ],
            what=f"create welcome issue for {username}",
        )
        return result.stdout.strip()
    except RuntimeError:
        return None


# =============================================================================
# Init Commands
# =============================================================================


def _init_team_yaml(repo_name: str, owner: str) -> Path:
    """Create a starter team.yaml file."""
    path = Path("team.yaml")
    if path.exists():
        raise FileExistsError(f"{path} already exists")
    
    content = TEAM_YAML_TEMPLATE.format(repo_name=repo_name, owner=owner)
    path.write_text(content)
    return path


def _init_github_action(multi_repo: bool = False) -> Path:
    """Create GitHub Action workflow for syncing collaborators."""
    workflows_dir = Path(".github/workflows")
    workflows_dir.mkdir(parents=True, exist_ok=True)
    
    if multi_repo:
        path = workflows_dir / "sync-team.yml"
        path.write_text(GITHUB_ACTION_MULTI_REPO_TEMPLATE)
        
        # Also create repos.txt if it doesn't exist
        repos_txt = Path("repos.txt")
        if not repos_txt.exists():
            repos_txt.write_text(REPOS_TXT_TEMPLATE)
    else:
        path = workflows_dir / "sync-collaborators.yml"
        path.write_text(GITHUB_ACTION_TEMPLATE)
    
    return path


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
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()
            except ValueError:
                continue
    raise ValueError(f"Cannot parse date: {value!r}")


def _parse_yaml_config(content: str, repo_owner: str, repo_name: str) -> TeamConfig:
    """Parse YAML team configuration."""
    data = yaml.safe_load(content)
    if not data:
        return TeamConfig()
    
    if not isinstance(data, dict):
        raise ValueError("YAML must be a dictionary")
    
    config = TeamConfig()
    config.default_permission = data.get("default_permission", "push")
    config.welcome_issue = data.get("welcome_issue", False)
    config.welcome_message = data.get("welcome_message")
    
    role_permissions = {
        "admins": "admin", "admin": "admin",
        "maintainers": "maintain", "maintainer": "maintain",
        "developers": "push", "developer": "push",
        "contributors": "push", "contributor": "push",
        "reviewers": "pull", "reviewer": "pull",
        "triagers": "triage", "triager": "triage",
        "readers": "pull", "reader": "pull",
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
            username=username, permission=permission, expires=expires, from_team=from_team,
        ))
    
    # Parse 'collaborators' key
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
                    if "/" in team_spec:
                        org, team_slug = team_spec.split("/", 1)
                        members = _get_team_members(org, team_slug)
                        for member in members:
                            add_collaborator(member, config.default_permission, from_team=team_spec)
                elif isinstance(team_spec, dict):
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
    
    users = _parse_usernames_txt(content)
    config = TeamConfig()
    for user in users:
        config.collaborators.append(Collaborator(username=user, permission="push"))
    return config


def _resolve_team_config(
    collab_spec: str, repo_owner: str, repo_name: str
) -> tuple[TeamConfig, str]:
    """Resolve team config from the spec."""
    repo_full_name = f"{repo_owner}/{repo_name}"
    default_files = ["team.yaml", "team.yml", "collaborators.yaml", "collaborators.yml", "collaborators.txt"]
    
    # Check if it's a remote repo reference (owner/repo format)
    # e.g., "michaeljabbour/madeteam" -> fetch team.yaml from that repo
    if "/" in collab_spec and not collab_spec.startswith(("./", "../", "local:", "repo:")):
        parts = collab_spec.split("/")
        if len(parts) == 2 and all(p.strip() for p in parts):
            source_owner, source_repo = parts
            # Try to fetch team.yaml from the source repo
            for filename in ["team.yaml", "team.yml"]:
                try:
                    content = _gh_read_repo_file(source_owner, source_repo, filename)
                    config = _parse_yaml_config(content, repo_owner, repo_name)
                    config.source = f"{source_owner}/{source_repo}:{filename}"
                    return config, config.source
                except RuntimeError as exc:
                    if "HTTP 404" not in str(exc):
                        raise
                    continue
            raise FileNotFoundError(f"team.yaml not found in {collab_spec}")
    
    # Explicit repo: prefix (reads from TARGET repo)
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

    raise FileNotFoundError(f"team config not found: {collab_spec}\n  hint: run 'addteam --init' to create one")


# =============================================================================
# Audit
# =============================================================================


def _audit_collaborators(
    config: TeamConfig, repo_owner: str, repo_name: str, me: str
) -> AuditResult:
    """Compare desired state (config) with actual state (GitHub)."""
    result = AuditResult()
    current = _get_collaborators_with_permissions(repo_owner, repo_name)
    
    desired: dict[str, Collaborator] = {}
    for collab in config.collaborators:
        if collab.username == repo_owner or collab.username == me:
            continue
        if collab.is_expired:
            result.expired.append(collab)
        else:
            desired[collab.username.casefold()] = collab
    
    for username_lower, collab in desired.items():
        found = False
        for current_user in current:
            if current_user.casefold() == username_lower:
                found = True
                current_perm = current[current_user]
                if current_perm != collab.permission:
                    result.permission_drift.append((current_user, current_perm, collab.permission))
                break
        if not found:
            result.missing.append(collab)
    
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
    *, provider: str, repo_full_name: str, repo_description: str, readme_content: str | None = None, timeout_s: int = 30
) -> str:
    """Generate an AI summary with install/usage instructions from README."""
    repo_url = f"https://github.com/{repo_full_name}"
    
    prompt_parts = [
        "Generate a concise, terminal-friendly onboarding summary for a GitHub repository.",
        "",
        "Audience: A developer who was just added as a collaborator",
        "Tone: Calm, friendly, practical - like a senior engineer explaining to a peer",
        "Formatting: Plain text, monospace-safe, NO emojis, NO markdown",
        "",
        f"Repo: {repo_full_name}",
        f"URL: {repo_url}",
        f"Description: {repo_description or '(none provided)'}",
    ]
    
    if readme_content:
        readme_excerpt = readme_content[:2500]
        if len(readme_content) > 2500:
            readme_excerpt += "\n... (truncated)"
        prompt_parts.extend([
            "",
            "README content:",
            "---",
            readme_excerpt,
            "---",
        ])
    
    prompt_parts.extend([
        "",
        "Output this EXACT structure (keep the labels, fill in the content):",
        "",
        f"{repo_full_name.split('/')[-1]}",
        f"{repo_url}",
        "",
        "What this is:",
        "<2-3 lines explaining the purpose and why it exists>",
        "",
        "What it does:",
        "- <concrete capability>",
        "- <concrete capability>",
        "- <concrete capability if relevant>",
        "",
        "Install:",
        "<single fastest install command - prefer uvx/pipx/npx if applicable>",
        "",
        "Quick start:",
        "<single working command to get started>",
        "",
        "RULES:",
        "- NO emojis anywhere",
        "- NO markdown formatting (no **, no ```, no headers)",
        "- NO fluff like 'Feel free to reach out' or 'Happy coding'",
        "- NO exclamation marks except maybe one",
        "- Assume reader has zero prior context",
        "- Focus on what the user can DO, not internals",
        "- Keep total output under 20 lines",
        "- If uvx/pipx works, prefer it over pip install",
        "",
        "Generate the summary now.",
    ])
    
    prompt = "\n".join(prompt_parts)

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
                "max_tokens": 500,
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
                "max_tokens": 500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout_s=timeout_s,
        )
        try:
            return response["content"][0]["text"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected Anthropic response: {response}") from exc

    if provider == "google":
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set")

        response = _http_post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            payload={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 500, "temperature": 0.2},
            },
            timeout_s=timeout_s,
        )
        try:
            return response["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected Google response: {response}") from exc

    if provider == "openrouter":
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")

        response = _http_post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"authorization": f"Bearer {api_key}"},
            payload={
                "model": "meta-llama/llama-3.1-8b-instruct:free",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 500,
                "temperature": 0.2,
            },
            timeout_s=timeout_s,
        )
        try:
            return response["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise RuntimeError(f"Unexpected OpenRouter response: {response}") from exc

    raise RuntimeError(f"Unknown provider: {provider}")


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
        epilog="""\
examples:
  addteam                         # use local team.yaml
  addteam owner/repo              # use team.yaml from another repo
  addteam -r owner/repo           # target specific repo
  addteam -n                      # dry-run (preview)
  addteam -a                      # audit mode
  addteam -s                      # sync mode
  addteam -i                      # create starter team.yaml
  addteam -i --init-action        # also create GitHub Action
""",
    )
    
    # Positional argument for config source (optional)
    parser.add_argument("source", nargs="?", default="team.yaml",
                        help="Config source: local file or owner/repo (default: team.yaml)")
    
    # Init commands (run before other args require gh)
    parser.add_argument("-i", "--init", action="store_true", help="Create starter team.yaml")
    parser.add_argument("--init-action", action="store_true", help="Create GitHub Action workflow")
    parser.add_argument("--init-multi-repo", action="store_true", help="Create multi-repo sync workflow")
    
    # Main options (keep -f as alias for backwards compatibility)
    parser.add_argument("-f", "--file", metavar="PATH", dest="source_override",
                        help="Config source (alternative to positional arg)")
    parser.add_argument("-u", "--user", metavar="NAME", help="Invite a single GitHub user")
    parser.add_argument("-p", "--permission", default="push", choices=list(VALID_PERMISSIONS),
                        help="Permission level (default: push)")
    parser.add_argument("-r", "--repo", metavar="OWNER/REPO", help="Target repo (default: current directory)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("-s", "--sync", action="store_true", help="Remove collaborators not in list")
    parser.add_argument("-a", "--audit", action="store_true", help="Show drift without making changes")
    parser.add_argument("--no-welcome", action="store_true", help="Skip creating welcome issues")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI-generated summary")
    parser.add_argument("--provider", default="auto", choices=["auto", "openai", "anthropic", "google", "openrouter"],
                        help="AI provider (default: auto)")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    args = parser.parse_args(argv)

    # ==========================================================================
    # INIT COMMANDS (don't require gh auth)
    # ==========================================================================
    
    if args.init or args.init_action or args.init_multi_repo:
        # Get repo info for init (optional, will use defaults if not in a repo)
        repo_name = "my-repo"
        owner = "your-username"
        
        try:
            view_args = ["repo", "view", "--json", "name,owner"]
            repo = _gh_json(view_args, what="get repo info")
            repo_name = repo["name"]
            owner = repo["owner"]["login"]
        except RuntimeError:
            pass  # Not in a repo or gh not authenticated, use defaults
        
        created_files = []
        
        if args.init:
            try:
                path = _init_team_yaml(repo_name, owner)
                created_files.append(str(path))
            except FileExistsError as exc:
                console.print(f"[yellow]skip:[/yellow] {exc}")
        
        if args.init_action:
            path = _init_github_action(multi_repo=False)
            created_files.append(str(path))
        
        if args.init_multi_repo:
            path = _init_github_action(multi_repo=True)
            created_files.append(str(path))
            if Path("repos.txt").exists():
                console.print("[dim]repos.txt already exists[/dim]")
            else:
                created_files.append("repos.txt")
        
        if created_files:
            console.print()
            console.print("[green]✓[/green] Created:")
            for f in created_files:
                console.print(f"  {f}")
            console.print()
            console.print("[dim]Next steps:[/dim]")
            console.print("  1. Edit team.yaml with your team members")
            console.print("  2. Commit and push")
            if args.init_action or args.init_multi_repo:
                console.print("  3. Add TEAM_SYNC_TOKEN secret to GitHub repo")
                console.print("     (PAT with repo + admin:org scopes)")
            console.print()
        
        return 0

    # ==========================================================================
    # VALIDATION
    # ==========================================================================

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

    # ==========================================================================
    # RESOLVE REPO
    # ==========================================================================

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
    
    mode = None
    if args.dry_run:
        mode = "dry-run"
    elif args.audit:
        mode = "audit"

    if not args.quiet:
        _print_header(repo_name, repo_owner, me, mode)
        _check_for_updates()

    # ==========================================================================
    # LOAD CONFIG
    # ==========================================================================

    if args.user:
        u = args.user.lstrip("@").strip()
        config = TeamConfig(
            collaborators=[Collaborator(u, args.permission)] if u else [],
            source=f"--user {u}",
        )
    else:
        # Use -f/--file if provided, otherwise use positional source argument
        config_source = args.source_override or args.source
        try:
            config, _ = _resolve_team_config(config_source, repo_owner, repo_name)
        except FileNotFoundError:
            # Friendly guidance instead of scary "error"
            console.print()
            console.print("  [yellow]No team.yaml found.[/yellow]")
            console.print()
            console.print("  [dim]Create one:[/dim]        addteam --init")
            console.print("  [dim]Use another repo:[/dim]  addteam owner/repo")
            console.print()
            return 0
        except (ValueError, RuntimeError) as exc:
            console.print(f"[red]error:[/red] {exc}")
            return 1

    # Welcome issues are ON by default, disable with --no-welcome
    if not args.no_welcome:
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

    # Generate AI summary upfront if needed for welcome issues
    ai_summary: str | None = None
    if config.welcome_issue and not args.no_ai:
        providers_to_try = []
        if args.provider != "auto":
            providers_to_try = [args.provider]
        else:
            # Priority order: OpenAI → Anthropic → Google → OpenRouter
            if os.getenv("OPENAI_API_KEY"):
                providers_to_try.append("openai")
            if os.getenv("ANTHROPIC_API_KEY"):
                providers_to_try.append("anthropic")
            if os.getenv("GOOGLE_API_KEY"):
                providers_to_try.append("google")
            if os.getenv("OPENROUTER_API_KEY"):
                providers_to_try.append("openrouter")
        
        if not providers_to_try:
            if not args.quiet:
                console.print("  [dim]ai[/dim]          no API keys found")
                console.print()
        else:
            # Fetch README for AI context
            readme_content = _get_readme_excerpt(repo_owner, repo_name, max_lines=100)
            
            for provider in providers_to_try:
                try:
                    ai_summary = _generate_repo_summary(
                        provider=provider,
                        repo_full_name=repo_full_name,
                        repo_description=description,
                        readme_content=readme_content,
                    )
                    if not args.quiet:
                        console.print(f"  [dim]ai[/dim]          {provider} ✓")
                        console.print()
                    break
                except Exception as e:
                    if not args.quiet:
                        console.print(f"  [dim]ai[/dim]          {provider} failed: {str(e)[:50]}")
                    continue
            
            if not ai_summary and not args.quiet:
                console.print()  # blank line after failed attempts

    # Fetch existing collaborators (accepted) and pending invitations
    try:
        existing_collabs = _get_collaborators_with_permissions(repo_owner, repo_name)
    except RuntimeError:
        existing_collabs = {}
    existing_lower = {u.casefold(): u for u in existing_collabs}
    
    pending_invites = _get_pending_invitations(repo_owner, repo_name)
    pending_lower = {u.casefold() for u in pending_invites}

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
        
        # Check if already has access (accepted invitation)
        if u.casefold() in existing_lower:
            results.append((u, "skip", "already has access"))
            skipped += 1
            continue
        
        # Check if already invited (pending)
        if u.casefold() in pending_lower:
            results.append((u, "skip", "already invited"))
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

        valid_users = {c.username.casefold() for c in config.collaborators if not c.is_expired}
        to_remove = sorted(u for u in current_collabs if u.casefold() not in valid_users)
        
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
        
        # Show AI summary at the end (useful for sharing via email/Slack)
        if ai_summary:
            if welcomed > 0:
                console.print("  [bold]Welcome message sent:[/bold]")
            else:
                console.print("  [bold]Repo summary (for sharing):[/bold]")
            console.print()
            for line in ai_summary.split("\n"):
                console.print(f"    {line}")
            console.print()

    return 1 if failed > 0 else 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
