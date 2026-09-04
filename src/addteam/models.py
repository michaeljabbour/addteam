"""Core data models for addteam."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

VALID_PERMISSIONS = {"pull", "triage", "push", "maintain", "admin"}

# GitHub's role_name values for org-less repos use read/write.
GITHUB_PERMISSION_MAP = {"read": "pull", "write": "push"}

# Role-based group names map to GitHub permissions.
ROLE_PERMISSIONS = {
    "admins": "admin",
    "admin": "admin",
    "maintainers": "maintain",
    "maintainer": "maintain",
    "developers": "push",
    "developer": "push",
    "contributors": "push",
    "contributor": "push",
    "contractors": "push",
    "contractor": "push",
    "reviewers": "pull",
    "reviewer": "pull",
    "triagers": "triage",
    "triager": "triage",
    "readers": "pull",
    "reader": "pull",
}


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
        return date.today() > self.expires  # noqa: DTZ011  expiry dates are naive by design


@dataclass
class TeamConfig:
    """Parsed team configuration from YAML or text file."""

    collaborators: list[Collaborator] = field(default_factory=list)
    default_permission: str = "push"
    # None = not set in config (CLI default applies). True/False = explicit choice.
    welcome_issue: bool | None = None
    welcome_message: str | None = None
    source: str = ""
    # Non-fatal problems found while resolving (unknown keys, team expansion failures).
    warnings: list[str] = field(default_factory=list)
    # True when part of the config could not be resolved (e.g. a team lookup failed).
    # --sync refuses to run with an incomplete config to avoid mass removals.
    incomplete: bool = False


@dataclass
class AuditResult:
    """Result of auditing current vs desired state."""

    missing: list[Collaborator] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    permission_drift: list[tuple[str, str, str]] = field(default_factory=list)
    expired: list[Collaborator] = field(default_factory=list)

    @property
    def drift_count(self) -> int:
        return len(self.missing) + len(self.extra) + len(self.permission_drift) + len(self.expired)
