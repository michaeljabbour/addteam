"""Team config parsing and cascading source resolution.

Resolution order for a spec:
  1. explicit prefixes: local:path, repo:path
  2. an existing local file (so "examples/team.yaml" is never mistaken for a repo)
  3. owner/repo — fetch team.yaml/team.yml from that repo
  4. default filenames locally, then in the target repo
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .gh import _get_team_members, _gh_read_repo_file
from .models import ROLE_PERMISSIONS, VALID_PERMISSIONS, Collaborator, TeamConfig, TeamMembershipSpec

# Top-level YAML keys addteam understands (plus the role group names).
KNOWN_CONFIG_KEYS = {
    "default_permission",
    "welcome_issue",
    "welcome_message",
    "collaborators",
    "teams",
} | set(ROLE_PERMISSIONS)

DEFAULT_CONFIG_FILES = ["team.yaml", "team.yml", "collaborators.yaml", "collaborators.yml", "collaborators.txt"]


# =============================================================================
# Path helpers
# =============================================================================


def _git_root() -> Path | None:
    try:
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
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
    # Windows drive paths (C:\..., C:/...)
    return len(value) >= 3 and value[1] == ":" and value[2] in ("/", "\\")


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


def _local_file_exists(spec: str) -> Path | None:
    """Return the resolved path if spec points at an existing local file."""
    resolved = _resolve_local_path(spec, prefer_repo_root=True)
    if resolved is not None and resolved.is_file():
        return resolved
    return None


# =============================================================================
# Parsing
# =============================================================================


def _parse_usernames_txt(text: str) -> list[str]:
    """Parse simple text file with one username per line."""
    seen: set[str] = set()
    users: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("@")
        if line not in seen:
            seen.add(line)
            users.append(line)
    return users


def _parse_date(value: Any) -> date | None:
    """Parse a date from various formats.

    Expiry dates are intentionally naive — they are compared against
    date.today(), never against timezone-aware datetimes.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(value, fmt).date()  # noqa: DTZ007  naive expiry dates are by design
            except ValueError:
                continue
        raise ValueError(f"Cannot parse date: {value!r}")
    raise TypeError(f"Cannot parse date of type {type(value).__name__}: {value!r}")


def _parse_yaml_config(content: str, repo_owner: str, repo_name: str) -> TeamConfig:
    """Parse YAML team configuration.

    Unknown top-level keys are collected as warnings instead of being silently
    ignored (e.g. a misspelled "develoeprs" must not drop people quietly).
    Team expansion failures are also warnings, but mark the config incomplete
    so --sync can refuse to run against partial state.
    """
    data = yaml.safe_load(content)
    if not data:
        return TeamConfig()

    if not isinstance(data, dict):
        raise TypeError("YAML must be a dictionary")

    config = TeamConfig()
    config.default_permission = data.get("default_permission", "push")
    if "welcome_issue" in data:
        config.welcome_issue = bool(data["welcome_issue"])
    config.welcome_message = data.get("welcome_message")

    for key in data:
        if key not in KNOWN_CONFIG_KEYS:
            config.warnings.append(f"unknown key {key!r} ignored (typo?)")

    by_username: dict[str, Collaborator] = {}

    def add_collaborator(
        username: str,
        permission: str,
        expires: date | None = None,
        from_team: str | None = None,
        name: str | None = None,
        group: str | None = None,
    ):
        username = username.lstrip("@").strip()
        if not username:
            return
        existing = by_username.get(username)
        if existing:
            # Already seen: keep the first (highest-permission) entry but
            # record every group the user appears in for --group filtering,
            # and adopt the display name if the first group didn't have one.
            if group:
                existing.groups.add(group)
            if existing.name is None and name:
                existing.name = name
            return
        if permission not in VALID_PERMISSIONS:
            permission = config.default_permission
        collab = Collaborator(
            username=username,
            permission=permission,
            expires=expires,
            from_team=from_team,
            name=name,
            groups={group} if group else set(),
        )
        by_username[username] = collab
        config.collaborators.append(collab)

    def _parse_item(item: Any, default_perm: str, group: str | None = None) -> None:
        """Parse a str-or-dict collaborator entry and add it.

        Entry shapes:
          - alice
          - username: alice + optional name/permission/expires
        Backwards compat: `name:` alone still acts as the username
        (its historical alias), and only doubles as a display name when
        `username:`/`user:` is present.
        """
        if isinstance(item, str):
            add_collaborator(item, default_perm, group=group)
        elif isinstance(item, dict):
            has_explicit_username = "username" in item or "user" in item
            username = item.get("username") or item.get("user") or item.get("name")
            display_name = item.get("name") if has_explicit_username else None
            if username:
                add_collaborator(
                    username,
                    item.get("permission", default_perm),
                    _parse_date(item.get("expires")),
                    name=display_name,
                    group=group,
                )

    def _expand_team(spec: str) -> list[str]:
        """Fetch team members, recording a warning instead of failing hard."""
        org, team_slug = spec.split("/", 1)
        try:
            return _get_team_members(org, team_slug)
        except RuntimeError as exc:
            config.warnings.append(str(exc))
            config.incomplete = True
            return []

    # Parse 'collaborators' key
    if "collaborators" in data:
        collabs = data["collaborators"]
        if isinstance(collabs, list):
            for item in collabs:
                _parse_item(item, config.default_permission, group="collaborators")

    # Parse role-based groups
    for role_key, permission in ROLE_PERMISSIONS.items():
        if role_key in data:
            role_data = data[role_key]
            if isinstance(role_data, list):
                for item in role_data:
                    _parse_item(item, permission, group=role_key)
            elif isinstance(role_data, dict):
                actual_perm = role_data.get("permission", permission)
                users = role_data.get("users", [])
                for user in users:
                    _parse_item(user, actual_perm, group=role_key)

    # Parse GitHub teams
    if "teams" in data:
        teams = data["teams"]
        if isinstance(teams, list):
            for team_spec in teams:
                if isinstance(team_spec, str):
                    if "/" in team_spec:
                        members = _expand_team(team_spec)
                        for member in members:
                            add_collaborator(member, config.default_permission, from_team=team_spec)
                elif isinstance(team_spec, dict):
                    for key, value in team_spec.items():
                        if "/" not in key:
                            continue
                        if isinstance(value, dict):
                            # Mapping form: org/slug: {permission, members, maintainers}.
                            # Repo access is still derived from CURRENT team members
                            # (same as the scalar form) — the members/maintainers
                            # lists below are the DESIRED roster addteam will
                            # reconcile the team itself against, a separate concern
                            # from repo-collaborator access.
                            perm = value.get("permission", config.default_permission)
                            if perm not in VALID_PERMISSIONS:
                                perm = config.default_permission
                            members_raw = value.get("members")
                            maintainers_raw = value.get("maintainers")
                            desired_members = (
                                [str(m).lstrip("@").strip() for m in members_raw]
                                if isinstance(members_raw, list)
                                else []
                            )
                            desired_maintainers = (
                                [str(m).lstrip("@").strip() for m in maintainers_raw]
                                if isinstance(maintainers_raw, list)
                                else []
                            )
                            org, slug = key.split("/", 1)
                            config.team_memberships.append(
                                TeamMembershipSpec(
                                    org=org,
                                    slug=slug,
                                    permission=perm,
                                    members=desired_members,
                                    maintainers=desired_maintainers,
                                )
                            )
                            for member in _expand_team(key):
                                add_collaborator(member, perm, from_team=key)
                        else:
                            perm = (
                                value
                                if isinstance(value, str) and value in VALID_PERMISSIONS
                                else config.default_permission
                            )
                            for member in _expand_team(key):
                                add_collaborator(member, perm, from_team=key)

    return config


def _load_team_config(path: Path, repo_owner: str, repo_name: str) -> TeamConfig:
    """Load team config from file, auto-detecting format."""
    content = path.read_text()

    is_yaml = (
        path.suffix in (".yaml", ".yml")
        or content.strip().startswith(("{", "[")) is False
        and (":" in content.split("\n")[0] if content.strip() else False)
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


# =============================================================================
# Cascading resolution
# =============================================================================


def _resolve_team_config(collab_spec: str, repo_owner: str, repo_name: str) -> tuple[TeamConfig, str]:
    """Resolve team config from the spec (see module docstring for order)."""
    repo_full_name = f"{repo_owner}/{repo_name}"

    # Explicit repo: prefix (reads from TARGET repo)
    if collab_spec.startswith("repo:"):
        repo_path = collab_spec.removeprefix("repo:").lstrip("/")
        if not repo_path:
            raise ValueError("repo path is empty")
        content = _gh_read_repo_file(repo_owner, repo_name, repo_path)
        config = (
            _parse_yaml_config(content, repo_owner, repo_name)
            if repo_path.endswith((".yaml", ".yml"))
            else TeamConfig(collaborators=[Collaborator(u, "push") for u in _parse_usernames_txt(content)])
        )
        config.source = f"{repo_full_name}:{repo_path}"
        return config, config.source

    explicit_local = collab_spec.startswith("local:")
    local_path = collab_spec.removeprefix("local:") if explicit_local else collab_spec
    if explicit_local and not local_path:
        raise ValueError("local path is empty")

    # A local file always wins over the owner/repo interpretation, so that
    # e.g. "examples/team.yaml" is never mistaken for a remote config repo.
    local_hit = _local_file_exists(local_path)
    if local_hit is not None:
        config = _load_team_config(local_hit, repo_owner, repo_name)
        config.source = f"local:{local_hit}"
        return config, config.source

    # Remote repo reference (owner/repo -> fetch team.yaml from that repo).
    # Explicitly relative paths (./ ../) are never remote references.
    if (
        not explicit_local
        and not local_path.startswith(("./", "../"))
        and _is_valid_repo_spec(local_path)
        and len(local_path.split("/")) == 2
    ):
        source_owner, source_repo = local_path.split("/")
        last_error: RuntimeError | None = None
        for filename in ["team.yaml", "team.yml"]:
            try:
                content = _gh_read_repo_file(source_owner, source_repo, filename)
                config = _parse_yaml_config(content, repo_owner, repo_name)
                config.source = f"{source_owner}/{source_repo}:{filename}"
                return config, config.source
            except RuntimeError as exc:
                if "HTTP 404" in str(exc):
                    continue
                last_error = exc
                raise
        if last_error is not None:
            raise last_error
        raise FileNotFoundError(f"team.yaml not found in {local_path} (and no local file at that path)")

    # Auto-resolve: try local first with multiple filenames
    files_to_try = [local_path] if local_path not in DEFAULT_CONFIG_FILES else list(DEFAULT_CONFIG_FILES)

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
            config = (
                _parse_yaml_config(content, repo_owner, repo_name)
                if repo_path.endswith((".yaml", ".yml"))
                else TeamConfig(collaborators=[Collaborator(u, "push") for u in _parse_usernames_txt(content)])
            )
            config.source = f"{repo_full_name}:{repo_path}"
            return config, config.source
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                continue
            raise

    raise FileNotFoundError(
        f"team config not found: {local_path}\n"
        "  hint: run 'addteam --init' (or 'addteam --init --from-current' to snapshot existing collaborators)"
    )
