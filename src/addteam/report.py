"""Directory-wide access report.

Scans every git repo found in a directory and produces a permission matrix:
who has access to which repos, at what level. Output: terminal table, CSV
(long or matrix layout), or JSON.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from .gh import _gh_api_paginated, _run
from .models import GITHUB_PERMISSION_MAP

CSV_FIELDS = ["repo", "username", "name", "permission", "status"]


@dataclass
class RepoAccess:
    """One collaborator's access to one repo."""

    repo: str  # owner/repo
    username: str
    permission: str
    status: str = "active"  # or "pending" (invited, not accepted)
    name: str = ""


@dataclass
class ReportResult:
    rows: list[RepoAccess] = field(default_factory=list)
    repos_seen: int = 0
    dirs_skipped: int = 0
    repo_failures: list[str] = field(default_factory=list)

    @property
    def usernames(self) -> list[str]:
        return sorted({r.username.casefold(): r.username for r in self.rows}.values(), key=str.casefold)

    @property
    def repos(self) -> list[str]:
        return sorted({r.repo for r in self.rows}, key=str.casefold)


def discover_repos(root: Path) -> tuple[list[Path], int]:
    """Immediate subdirectories that are git working copies (+ count of others)."""
    subdirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    repos = [p for p in subdirs if (p / ".git").exists()]
    return repos, len(subdirs) - len(repos)


def _repo_slug(path: Path) -> str | None:
    """Resolve owner/repo for a working copy (via its git remote)."""
    result = _run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        cwd=path,
    )
    if result.returncode != 0:
        return None
    slug = result.stdout.strip()
    return slug if "/" in slug else None


def _repo_access(slug: str) -> list[RepoAccess]:
    """Accepted collaborators and pending invitations for owner/repo."""
    rows: list[RepoAccess] = []
    items = _gh_api_paginated(
        [f"repos/{slug}/collaborators", "-f", "affiliation=direct"],
        what=f"fetch collaborators for {slug}",
    )
    for item in items:
        login = item.get("login", "")
        if not login:
            continue
        perm = item.get("role_name") or "read"
        rows.append(RepoAccess(repo=slug, username=login, permission=GITHUB_PERMISSION_MAP.get(perm, perm)))

    try:
        invites = _gh_api_paginated([f"repos/{slug}/invitations"], what=f"fetch invitations for {slug}")
    except RuntimeError:
        invites = []  # needs admin rights; skip silently, active rows are the ground truth
    for item in invites:
        invitee = item.get("invitee") or {}
        login = invitee.get("login", "")
        if not login:
            continue
        perm = item.get("permissions") or "read"
        rows.append(
            RepoAccess(
                repo=slug,
                username=login,
                permission=GITHUB_PERMISSION_MAP.get(perm, perm),
                status="pending",
            )
        )
    return rows


def _attach_names(result: ReportResult) -> None:
    """Fill RepoAccess.name from GitHub profiles (one call per unique user)."""
    by_login: dict[str, list[RepoAccess]] = {}
    for row in result.rows:
        by_login.setdefault(row.username, []).append(row)
    for login in sorted(by_login, key=str.casefold):
        r = _run(["gh", "api", f"users/{login}", "--jq", '.name // ""'])
        if r.returncode != 0:
            continue
        display = r.stdout.strip()
        if display:
            for row in by_login[login]:
                row.name = display


def build_report(root: Path, *, include_names: bool = True) -> ReportResult:
    """Scan all repos directly under `root` and collect who has access."""
    result = ReportResult()
    repo_dirs, result.dirs_skipped = discover_repos(root)
    for path in repo_dirs:
        slug = _repo_slug(path)
        if slug is None:
            result.repo_failures.append(path.name)
            continue
        result.repos_seen += 1
        try:
            result.rows.extend(_repo_access(slug))
        except RuntimeError:
            result.repo_failures.append(slug)
    if include_names and result.rows:
        _attach_names(result)
    return result


def write_long_csv(result: ReportResult, path: Path) -> None:
    """One row per (repo, user) — good for filtering/pivoting."""
    rows = sorted(result.rows, key=lambda r: (r.repo.casefold(), r.username.casefold()))
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "repo": r.repo,
                    "username": r.username,
                    "name": r.name,
                    "permission": r.permission,
                    "status": r.status,
                }
            )


def write_matrix_csv(result: ReportResult, path: Path) -> None:
    """Users x repos permission matrix — good for reviewing at a glance."""
    repos = result.repos
    cell: dict[tuple[str, str], list[str]] = {}
    for r in result.rows:
        key = (r.username.casefold(), r.repo)
        label = f"{r.permission} (pending)" if r.status == "pending" else r.permission
        cell.setdefault(key, []).append(label)

    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["username", "name", *repos])
        for username in result.usernames:
            name = next((r.name for r in result.rows if r.username == username), "")
            row = [username, name]
            for repo in repos:
                labels = sorted(set(cell.get((username.casefold(), repo), [])))
                row.append(", ".join(labels))
            writer.writerow(row)


def matrix_lines(result: ReportResult) -> tuple[list[str], list[list[str]]]:
    """Header + rows for the terminal permission matrix (users x repos)."""
    repos = [repo.split("/", 1)[-1] for repo in result.repos]  # short name when owner repeats
    header = ["user", *repos]
    cell: dict[tuple[str, str], list[str]] = {}
    for r in result.rows:
        key = (r.username.casefold(), r.repo)
        label = "~" if r.status == "pending" else r.permission
        cell.setdefault(key, []).append(label)
    lines: list[list[str]] = []
    for username in result.usernames:
        line = [username]
        for full in result.repos:
            labels = sorted(set(cell.get((username.casefold(), full), [])))
            line.append("/".join(labels) if labels else "·")
        lines.append(line)
    return header, lines
