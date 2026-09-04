"""Backwards-compatible import surface.

The implementation was split into focused modules (config, gh, ai, ui, app).
Anything previously importable from `addteam.bootstrap_repo` keeps working
here so external scripts (scripts/bootstrap_repo.py, user integrations) do
not break.
"""

from __future__ import annotations

from . import __version__
from .ai import (
    _AI_PROVIDERS,
    _ai_extract,
    _ai_request,
    _generate_repo_summary,
    _http_post_json,
)
from .app import (
    _audit_collaborators,
    _handle_apply,
    _handle_audit,
    _handle_init,
    run,
)
from .config import (
    _is_valid_repo_spec,
    _load_team_config,
    _looks_like_local_path,
    _parse_date,
    _parse_usernames_txt,
    _parse_yaml_config,
    _resolve_local_path,
    _resolve_team_config,
    _split_repo_spec,
)
from .console import console, err_console
from .gh import (
    _create_welcome_issue,
    _get_collaborators_with_permissions,
    _get_pending_invitations,
    _get_readme_excerpt,
    _get_repo_info,
    _get_team_members,
    _gh_json,
    _gh_read_repo_file,
    _gh_text,
    _run,
    _run_checked,
)
from .models import (
    ROLE_PERMISSIONS,
    VALID_PERMISSIONS,
    AuditResult,
    Collaborator,
    TeamConfig,
)
from .templates import (
    GITHUB_ACTION_MULTI_REPO_TEMPLATE,
    GITHUB_ACTION_TEMPLATE,
    REPOS_TXT_TEMPLATE,
    TEAM_YAML_TEMPLATE,
)
from .ui import check_for_updates as _check_for_updates

__all__ = [
    "GITHUB_ACTION_MULTI_REPO_TEMPLATE",
    "GITHUB_ACTION_TEMPLATE",
    "REPOS_TXT_TEMPLATE",
    "ROLE_PERMISSIONS",
    "TEAM_YAML_TEMPLATE",
    "VALID_PERMISSIONS",
    "_AI_PROVIDERS",
    "AuditResult",
    "Collaborator",
    "TeamConfig",
    "__version__",
    "_ai_extract",
    "_ai_request",
    "_audit_collaborators",
    "_check_for_updates",
    "_create_welcome_issue",
    "_generate_repo_summary",
    "_get_collaborators_with_permissions",
    "_get_pending_invitations",
    "_get_readme_excerpt",
    "_get_repo_info",
    "_get_team_members",
    "_gh_json",
    "_gh_read_repo_file",
    "_gh_text",
    "_handle_apply",
    "_handle_audit",
    "_handle_init",
    "_http_post_json",
    "_is_valid_repo_spec",
    "_load_team_config",
    "_looks_like_local_path",
    "_parse_date",
    "_parse_usernames_txt",
    "_parse_yaml_config",
    "_resolve_local_path",
    "_resolve_team_config",
    "_run",
    "_run_checked",
    "_split_repo_spec",
    "console",
    "err_console",
    "run",
]
