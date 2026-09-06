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
    """A collaborator with permission, optional expiry, and display name."""

    username: str
    permission: str = "push"
    expires: date | None = None
    from_team: str | None = None
    name: str | None = None  # real/display name, e.g. for welcome issues
    groups: set[str] = field(default_factory=set)  # all config groups this user appears in

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
    # Mapping-form `teams:` entries whose org team roster addteam should manage.
    team_memberships: list[TeamMembershipSpec] = field(default_factory=list)


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


@dataclass
class TeamMembershipSpec:
    """A `teams:` mapping-form entry: org team membership addteam should manage
    (in addition to using the team's current members for repo access, which
    is unchanged, existing behavior — see config.py's `_expand_team` usage)."""

    org: str
    slug: str
    permission: str
    members: list[str] = field(default_factory=list)
    maintainers: list[str] = field(default_factory=list)

    @property
    def team_spec(self) -> str:
        return f"{self.org}/{self.slug}"


@dataclass
class TeamMembershipAudit:
    """Drift between a TeamMembershipSpec's desired roster and GitHub's actual one."""

    spec: TeamMembershipSpec
    missing_members: list[str] = field(default_factory=list)
    missing_maintainers: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    role_mismatches: list[str] = field(default_factory=list)  # desired maintainer, actually plain member
    total_current: int = 0  # size of the actual roster (member+maintainer), for the circuit breaker

    @property
    def drift_count(self) -> int:
        return len(self.missing_members) + len(self.missing_maintainers) + len(self.extra) + len(self.role_mismatches)


@dataclass
class TeamMembershipResult:
    """Outcome of _apply_team_memberships: what happened to org team rosters this run."""

    ensured: list[tuple[str, str, str]] = field(default_factory=list)  # (team, username, role)
    ensure_failed: list[tuple[str, str, str, str]] = field(default_factory=list)  # (team, username, role, detail)
    would_ensure: list[tuple[str, str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)  # (team, username)
    remove_failed: list[tuple[str, str, str]] = field(default_factory=list)  # (team, username, detail)
    would_remove: list[tuple[str, str]] = field(default_factory=list)
    team_errors: list[tuple[str, str]] = field(default_factory=list)  # (team, message) - unreadable/blocked teams
    skipped_personal_repo: bool = False
