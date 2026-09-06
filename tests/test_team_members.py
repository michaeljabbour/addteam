# SPDX-License-Identifier: MIT
"""v1.6.0: teams: mapping form — org team membership management.

Covers config parsing of the mapping form (`org/slug: {permission, members,
maintainers}`), drift computation in `_audit_team_membership`, and the
ensure/remove behavior of `_apply_team_memberships` plus its integration
into `_handle_apply` and `_handle_audit`.
"""

import json
from unittest.mock import MagicMock, patch

from conftest import _audit_args, _make_args, _make_paginated_router

from addteam.app import _apply_team_memberships, _audit_team_membership, _handle_apply, _handle_audit
from addteam.config import _parse_yaml_config
from addteam.models import AuditResult, TeamConfig, TeamMembershipAudit, TeamMembershipSpec


def _spec(members=None, maintainers=None) -> TeamMembershipSpec:
    return TeamMembershipSpec(
        org="myorg",
        slug="backend",
        permission="push",
        members=members or [],
        maintainers=maintainers or [],
    )


def _config_with_specs(*specs) -> TeamConfig:
    return TeamConfig(collaborators=[], team_memberships=list(specs))


def _team_audit(spec, **kwargs) -> TeamMembershipAudit:
    return TeamMembershipAudit(spec=spec, **kwargs)


# =============================================================================
# Config parsing: mapping form
# =============================================================================


@patch("addteam.config._get_team_members", return_value=["alice", "bob"])
def test_mapping_form_parses_spec_and_expands_members(mock_members):
    yaml = "teams:\n  - myorg/backend:\n      permission: push\n      members: [alice]\n      maintainers: [bob]\n"
    config = _parse_yaml_config(yaml, "owner", "repo")
    assert config.team_memberships == [
        TeamMembershipSpec(org="myorg", slug="backend", permission="push", members=["alice"], maintainers=["bob"])
    ]
    # Repo access still derived from CURRENT team members, unchanged.
    perms = {c.username: c.permission for c in config.collaborators}
    assert perms == {"alice": "push", "bob": "push"}


@patch("addteam.config._get_team_members", return_value=["alice"])
def test_mapping_form_invalid_permission_falls_back_to_default(mock_members):
    yaml = "teams:\n  - myorg/backend:\n      permission: bogus\n      members: [alice]\n"
    config = _parse_yaml_config(yaml, "owner", "repo")
    assert len(config.team_memberships) == 1
    assert config.team_memberships[0].permission == config.default_permission


@patch("addteam.config._get_team_members", return_value=[])
def test_mapping_form_non_list_members_tolerated(mock_members):
    yaml = "teams:\n  - myorg/backend:\n      permission: push\n      members: alice\n"
    config = _parse_yaml_config(yaml, "owner", "repo")
    assert len(config.team_memberships) == 1
    assert config.team_memberships[0].members == []


@patch("addteam.config._get_team_members", return_value=["alice"])
def test_scalar_form_still_works_unchanged(mock_members):
    config = _parse_yaml_config("teams:\n  - myorg/frontend: pull\n", "owner", "repo")
    assert config.team_memberships == []
    assert [(c.username, c.permission) for c in config.collaborators] == [("alice", "pull")]


@patch("addteam.gh._gh_api_paginated")
def test_audit_team_membership_computes_drift(mock_paginated):
    mock_paginated.side_effect = _make_paginated_router(
        {
            "role=maintainer": [{"login": "bob"}],
            "role=member": [{"login": "alice"}, {"login": "eve"}],
        }
    )
    spec = _spec(members=["alice", "carol"], maintainers=["bob"])
    audit = _audit_team_membership(spec)
    assert audit.missing_members == ["carol"]
    assert audit.missing_maintainers == []
    assert audit.extra == ["eve"]
    assert audit.role_mismatches == []
    assert audit.total_current == 3


@patch("addteam.gh._gh_api_paginated")
def test_audit_team_membership_detects_role_mismatch(mock_paginated):
    """Desired maintainer who is actually a plain member is a role mismatch."""
    mock_paginated.side_effect = _make_paginated_router(
        {
            "role=maintainer": [],
            "role=member": [{"login": "alice"}, {"login": "bob"}],
        }
    )
    spec = _spec(members=["alice"], maintainers=["bob"])
    audit = _audit_team_membership(spec)
    # Implementation counts this user in both: not actually a maintainer
    # (missing_maintainers) AND present with the wrong role (role_mismatches).
    assert audit.role_mismatches == ["bob"]
    assert audit.missing_members == []
    assert audit.missing_maintainers == ["bob"]


@patch("addteam.gh._gh_api_paginated")
def test_audit_team_membership_read_failure_raises(mock_paginated):
    mock_paginated.side_effect = RuntimeError("HTTP 403")
    spec = _spec(members=["alice"])
    try:
        _audit_team_membership(spec)
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "requires org admin or team maintainer" in str(exc)


# =============================================================================
# _apply_team_memberships
# =============================================================================


@patch("addteam.app._run", return_value=MagicMock(returncode=0, stderr="", stdout=""))
@patch("addteam.app._audit_team_membership")
def test_apply_ensures_missing_members_and_maintainers(mock_audit, mock_run):
    spec = _spec(members=["carol"], maintainers=["dana"])
    mock_audit.return_value = _team_audit(spec, missing_members=["carol"], missing_maintainers=["dana"])
    config = _config_with_specs(spec)

    result = _apply_team_memberships(_make_args(), config, is_personal_repo=False, human=False)

    puts = [c for c in mock_run.call_args_list if "-X" in c.args[0] and "PUT" in c.args[0]]
    assert len(puts) == 2
    joined = [" ".join(c.args[0]) for c in puts]
    assert any("memberships/carol" in j and "role=member" in j for j in joined)
    assert any("memberships/dana" in j and "role=maintainer" in j for j in joined)
    assert ("myorg/backend", "carol", "member") in result.ensured
    assert ("myorg/backend", "dana", "maintainer") in result.ensured


@patch("addteam.app._run")
@patch("addteam.app._audit_team_membership")
def test_apply_dry_run_previews_ensure_only(mock_audit, mock_run):
    spec = _spec(members=["carol"])
    mock_audit.return_value = _team_audit(spec, missing_members=["carol"])
    config = _config_with_specs(spec)

    result = _apply_team_memberships(_make_args(dry_run=True), config, is_personal_repo=False, human=False)

    mock_run.assert_not_called()
    assert ("myorg/backend", "carol", "member") in result.would_ensure
    assert result.ensured == []


@patch("addteam.app._run")
@patch("addteam.app._audit_team_membership")
def test_apply_skips_personal_repo(mock_audit, mock_run):
    spec = _spec(members=["carol"])
    config = _config_with_specs(spec)

    result = _apply_team_memberships(_make_args(), config, is_personal_repo=True, human=False)

    assert result.skipped_personal_repo is True
    mock_audit.assert_not_called()
    mock_run.assert_not_called()


@patch("addteam.app._run")
@patch("addteam.app._audit_team_membership")
def test_apply_no_removal_without_sync_teams(mock_audit, mock_run):
    spec = _spec(members=["alice"])
    mock_audit.return_value = _team_audit(spec, extra=["eve"], total_current=2)
    config = _config_with_specs(spec)

    result = _apply_team_memberships(_make_args(sync_teams=False), config, is_personal_repo=False, human=False)

    deletes = [c for c in mock_run.call_args_list if "DELETE" in c.args[0]]
    assert deletes == []
    assert result.removed == []
    assert result.would_remove == []


@patch("addteam.app._run", return_value=MagicMock(returncode=0, stderr="", stdout=""))
@patch("addteam.app._audit_team_membership")
def test_apply_removes_extra_with_sync_teams(mock_audit, mock_run):
    spec = _spec(members=["alice"])
    mock_audit.return_value = _team_audit(spec, extra=["eve"], total_current=5)
    config = _config_with_specs(spec)

    args = _make_args(sync_teams=True, yes=True)
    result = _apply_team_memberships(args, config, is_personal_repo=False, human=False)

    deletes = [c for c in mock_run.call_args_list if "DELETE" in c.args[0]]
    assert len(deletes) == 1
    assert "memberships/eve" in " ".join(deletes[0].args[0])
    assert result.removed == [("myorg/backend", "eve")]


@patch("addteam.app._run", return_value=MagicMock(returncode=0, stderr="", stdout=""))
@patch("addteam.app._audit_team_membership")
def test_apply_removal_blocked_by_circuit_breaker(mock_audit, mock_run):
    spec = _spec(members=["alice"])
    extras = ["e1", "e2", "e3", "e4", "e5"]
    mock_audit.return_value = _team_audit(spec, extra=extras, total_current=10)
    config = _config_with_specs(spec)

    args = _make_args(sync_teams=True, yes=True, max_removals=3)
    result = _apply_team_memberships(args, config, is_personal_repo=False, human=False)

    deletes = [c for c in mock_run.call_args_list if "DELETE" in c.args[0]]
    assert deletes == []
    assert result.removed == []
    assert len(result.team_errors) == 1
    assert result.team_errors[0][0] == "myorg/backend"
    assert "refusing to remove" in result.team_errors[0][1]


@patch("addteam.app._run", return_value=MagicMock(returncode=0, stderr="", stdout=""))
@patch("addteam.app._audit_team_membership")
def test_apply_removal_allow_mass_removal_bypasses(mock_audit, mock_run):
    spec = _spec(members=["alice"])
    extras = ["e1", "e2", "e3", "e4", "e5"]
    mock_audit.return_value = _team_audit(spec, extra=extras, total_current=10)
    config = _config_with_specs(spec)

    args = _make_args(sync_teams=True, yes=True, max_removals=3, allow_mass_removal=True)
    result = _apply_team_memberships(args, config, is_personal_repo=False, human=False)

    deletes = [c for c in mock_run.call_args_list if "DELETE" in c.args[0]]
    assert len(deletes) == 5
    assert result.removed == [("myorg/backend", f"e{i}") for i in range(1, 6)]
    assert result.team_errors == []


@patch("addteam.app._run", return_value=MagicMock(returncode=0, stderr="", stdout=""))
@patch("addteam.app._audit_team_membership")
def test_apply_team_read_failure_marks_incomplete_no_removal(mock_audit, mock_run):
    bad = _spec(members=["alice"])
    good = TeamMembershipSpec(org="myorg", slug="frontend", permission="push", members=["carol"])

    def _audit_side_effect(spec):
        if spec.slug == "backend":
            raise RuntimeError("could not read membership for team myorg/backend (HTTP 403)")
        return _team_audit(spec, missing_members=["carol"])

    mock_audit.side_effect = _audit_side_effect
    config = _config_with_specs(bad, good)

    result = _apply_team_memberships(_make_args(sync_teams=True, yes=True), config, is_personal_repo=False, human=False)

    assert len(result.team_errors) == 1
    assert result.team_errors[0][0] == "myorg/backend"
    # Nothing touched on the failing team; other teams unaffected.
    calls = [" ".join(c.args[0]) for c in mock_run.call_args_list]
    assert not any("teams/backend" in j for j in calls)
    assert ("myorg/frontend", "carol", "member") in result.ensured


# =============================================================================
# _handle_apply / _handle_audit integration
# =============================================================================

_HANDLE_APPLY_PATCHES = (
    patch("addteam.app._get_pending_invitations", return_value={}),
    patch("addteam.app._get_collaborators_with_permissions", return_value={}),
)


@patch("addteam.app._audit_team_membership")
def test_handle_apply_exit_code_reflects_team_failures(mock_audit):
    mock_audit.side_effect = RuntimeError("could not read membership for team myorg/backend (HTTP 403)")
    config = _config_with_specs(_spec(members=["alice"]))
    mock_pending, mock_collabs = _HANDLE_APPLY_PATCHES
    with mock_pending, mock_collabs:
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
    assert result == 1


@patch("addteam.app._run", return_value=MagicMock(returncode=0, stderr="", stdout=""))
@patch("addteam.app._audit_team_membership")
def test_handle_apply_json_includes_team_memberships(mock_audit, mock_run, capsys):
    spec = _spec(members=["carol"])
    mock_audit.return_value = _team_audit(spec, missing_members=["carol"])
    config = _config_with_specs(spec)
    mock_pending, mock_collabs = _HANDLE_APPLY_PATCHES
    with mock_pending, mock_collabs:
        result = _handle_apply(_make_args(json=True), config, "owner", "repo", "owner/repo", "", "me")
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["team_memberships"]["ensured"] == [{"team": "myorg/backend", "username": "carol", "role": "member"}]


@patch("addteam.app._audit_team_membership")
@patch("addteam.app._audit_collaborators")
def test_handle_audit_no_drift_requires_team_membership_clean_too(mock_collab_audit, mock_team_audit, capsys):
    mock_collab_audit.return_value = AuditResult()  # repo side clean
    spec = _spec(members=["carol"])
    mock_team_audit.return_value = _team_audit(spec, missing_members=["carol"])
    config = _config_with_specs(spec)

    result = _handle_audit(config, "owner", "repo", "me", _audit_args())

    assert result == 0  # audit is informational without --fail-on-drift
    out = capsys.readouterr().out
    assert "no drift detected" not in out
    assert "Team membership" in out
    assert "carol" in out


@patch("addteam.app._audit_team_membership")
@patch("addteam.app._audit_collaborators")
def test_handle_audit_json_includes_team_memberships_and_errors(mock_collab_audit, mock_team_audit, capsys):
    mock_collab_audit.return_value = AuditResult()
    good = _spec(members=["alice"])
    bad = TeamMembershipSpec(org="myorg", slug="frontend", permission="push", members=["carol"])

    def _audits(spec):
        if spec.slug == "frontend":
            raise RuntimeError("could not read membership (HTTP 403)")
        return _team_audit(spec, missing_members=["alice"])

    mock_team_audit.side_effect = _audits
    config = _config_with_specs(good, bad)

    result = _handle_audit(config, "owner", "repo", "me", _audit_args(json=True))

    assert result == 1  # team-audit errors force exit 1 even though audit is informational
    payload = json.loads(capsys.readouterr().out)
    assert payload["team_memberships"] == [
        {
            "team": "myorg/backend",
            "missing_members": ["alice"],
            "missing_maintainers": [],
            "extra": [],
            "role_mismatches": [],
        }
    ]
    assert len(payload["team_membership_errors"]) == 1
    assert payload["team_membership_errors"][0]["team"] == "myorg/frontend"


@patch("addteam.app._audit_team_membership")
@patch("addteam.app._audit_collaborators")
def test_handle_audit_skips_personal_repo_with_note(mock_collab_audit, mock_team_audit, capsys):
    mock_collab_audit.return_value = AuditResult()
    config = _config_with_specs(_spec(members=["alice"]))

    result = _handle_audit(config, "owner", "repo", "me", _audit_args(), is_personal_repo=True)

    assert result == 0
    mock_team_audit.assert_not_called()
    out = capsys.readouterr().out
    assert "skipped (personal repo)" in out
    assert "no drift detected" in out  # repo side clean; team state ignored
