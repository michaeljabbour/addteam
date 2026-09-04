"""Tests for addteam (split modules: models/config/gh/ai/ui/app)."""

import argparse
import json
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import httpx
import pytest

from addteam.ai import _generate_repo_summary
from addteam.app import (
    _audit_collaborators,
    _handle_apply,
    _handle_audit,
    _handle_init,
    _resolve_welcome,
    run,
)
from addteam.config import (
    _is_valid_repo_spec,
    _looks_like_local_path,
    _parse_date,
    _parse_usernames_txt,
    _parse_yaml_config,
    _resolve_team_config,
)
from addteam.gh import (
    _create_welcome_issue,
    _get_collaborators_with_permissions,
    _get_pending_invitations,
    _get_team_members,
)
from addteam.models import AuditResult, Collaborator, TeamConfig


def _today() -> date:
    """Naive local today — matches what the app compares expiry dates against."""
    return date.today()  # noqa: DTZ011


# =============================================================================
# Data Model Tests
# =============================================================================


class TestCollaborator:
    """Tests for Collaborator dataclass."""

    def test_not_expired_when_no_date(self):
        c = Collaborator(username="alice")
        assert not c.is_expired

    def test_not_expired_when_future(self):
        future = _today() + timedelta(days=30)
        c = Collaborator(username="alice", expires=future)
        assert not c.is_expired

    def test_expired_when_past(self):
        past = _today() - timedelta(days=1)
        c = Collaborator(username="alice", expires=past)
        assert c.is_expired

    def test_default_permission(self):
        c = Collaborator(username="alice")
        assert c.permission == "push"


class TestTeamConfig:
    """Tests for TeamConfig dataclass."""

    def test_defaults(self):
        config = TeamConfig()
        assert config.collaborators == []
        assert config.default_permission == "push"
        # None = unset in config; run() resolves the actual default.
        assert config.welcome_issue is None
        assert config.welcome_message is None
        assert config.source == ""
        assert config.warnings == []
        assert config.incomplete is False


# =============================================================================
# Parser Tests
# =============================================================================


class TestParseUsernamesTxt:
    """Tests for _parse_usernames_txt."""

    def test_simple_list(self):
        text = "alice\nbob\ncharlie"
        assert _parse_usernames_txt(text) == ["alice", "bob", "charlie"]

    def test_strips_at_signs(self):
        text = "@alice\n@bob"
        assert _parse_usernames_txt(text) == ["alice", "bob"]

    def test_ignores_comments(self):
        text = "alice\n# comment\nbob"
        assert _parse_usernames_txt(text) == ["alice", "bob"]

    def test_ignores_blank_lines(self):
        text = "alice\n\n\nbob"
        assert _parse_usernames_txt(text) == ["alice", "bob"]

    def test_strips_whitespace(self):
        text = "  alice  \n  bob  "
        assert _parse_usernames_txt(text) == ["alice", "bob"]

    def test_deduplicates(self):
        text = "alice\nbob\nalice"
        assert _parse_usernames_txt(text) == ["alice", "bob"]


class TestParseDate:
    """Tests for _parse_date."""

    def test_none_returns_none(self):
        assert _parse_date(None) is None

    def test_date_passthrough(self):
        d = date(2025, 6, 1)
        assert _parse_date(d) == d

    def test_iso_format(self):
        assert _parse_date("2025-06-01") == date(2025, 6, 1)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_date("not-a-date")


class TestParseYamlConfig:
    """Tests for _parse_yaml_config."""

    def test_empty_yaml(self):
        config = _parse_yaml_config("", "owner", "repo")
        assert config.collaborators == []

    def test_simple_admins(self):
        yaml = """
admins:
  - alice
  - bob
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert len(config.collaborators) == 2
        assert config.collaborators[0].username == "alice"
        assert config.collaborators[0].permission == "admin"

    def test_developers_get_push(self):
        yaml = """
developers:
  - charlie
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert config.collaborators[0].permission == "push"

    def test_reviewers_get_pull(self):
        yaml = """
reviewers:
  - eve
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert config.collaborators[0].permission == "pull"

    def test_collaborators_with_expiry(self):
        yaml = """
developers:
  - username: temp-dev
    expires: 2025-06-01
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert config.collaborators[0].expires == date(2025, 6, 1)

    def test_welcome_issue_setting(self):
        yaml = """
welcome_issue: true
developers:
  - alice
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert config.welcome_issue is True

    def test_default_permission(self):
        yaml = """
default_permission: admin
collaborators:
  - alice
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert config.collaborators[0].permission == "admin"


# =============================================================================
# Utility Tests
# =============================================================================


class TestIsValidRepoSpec:
    """Tests for _is_valid_repo_spec."""

    def test_valid_owner_repo(self):
        assert _is_valid_repo_spec("owner/repo") is True

    def test_valid_host_owner_repo(self):
        assert _is_valid_repo_spec("github.com/owner/repo") is True

    def test_invalid_single_part(self):
        assert _is_valid_repo_spec("repo") is False

    def test_invalid_trailing_slash(self):
        assert _is_valid_repo_spec("owner/repo/") is False

    def test_invalid_empty(self):
        assert _is_valid_repo_spec("") is False


class TestLooksLikeLocalPath:
    """Tests for _looks_like_local_path."""

    def test_absolute_unix(self):
        assert _looks_like_local_path("/path/to/file") is True

    def test_relative_dot(self):
        assert _looks_like_local_path("./file") is True

    def test_relative_dotdot(self):
        assert _looks_like_local_path("../file") is True

    def test_home_tilde(self):
        assert _looks_like_local_path("~/file") is True

    def test_not_a_path(self):
        assert _looks_like_local_path("owner/repo") is False


# =============================================================================
# CLI Tests
# =============================================================================


class TestRun:
    """Tests for run() CLI function."""

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            run(["--version"])
        assert exc.value.code == 0

    def test_invalid_repo(self, capsys):
        result = run(["--repo", "invalid"])
        assert result == 2

    @patch("addteam.app.shutil.which")
    def test_gh_not_found(self, mock_which, capsys):
        mock_which.return_value = None
        result = run(["owner/repo"])
        assert result == 1
        captured = capsys.readouterr()
        assert "gh" in captured.err.lower()

    @patch("addteam.app.shutil.which")
    @patch("addteam.gh._run_checked")
    def test_init_creates_team_yaml(self, mock_run, mock_which, tmp_path, monkeypatch):
        mock_which.return_value = "/usr/bin/gh"
        mock_run.side_effect = RuntimeError("not in repo")

        monkeypatch.chdir(tmp_path)
        result = run(["--init"])

        assert result == 0
        assert (tmp_path / "team.yaml").exists()

    @patch("addteam.app.shutil.which")
    @patch("addteam.gh._run_checked")
    def test_init_action_creates_workflow(self, mock_run, mock_which, tmp_path, monkeypatch):
        mock_which.return_value = "/usr/bin/gh"
        mock_run.side_effect = RuntimeError("not in repo")

        monkeypatch.chdir(tmp_path)
        result = run(["--init-action"])

        assert result == 0
        assert (tmp_path / ".github" / "workflows" / "sync-collaborators.yml").exists()


# =============================================================================
# Integration Tests (require mocking gh)
# =============================================================================


class TestDryRun:
    """Tests for dry-run mode."""

    @patch("addteam.app.shutil.which")
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._gh_json")
    @patch("addteam.app._gh_text")
    def test_dry_run_shows_preview(
        self, mock_text, mock_json, mock_collabs, mock_pending, mock_which, tmp_path, monkeypatch, capsys
    ):
        mock_which.return_value = "/usr/bin/gh"
        mock_json.return_value = {"name": "repo", "owner": {"login": "owner"}, "description": "test"}
        mock_text.return_value = "me"

        # Create team.yaml
        team_yaml = tmp_path / "team.yaml"
        team_yaml.write_text("developers:\n  - alice\n")

        monkeypatch.chdir(tmp_path)
        result = run(["--dry-run", "--no-welcome"])

        assert result == 0
        captured = capsys.readouterr()
        assert "alice" in captured.out
        assert "would" in captured.out.lower() or "○" in captured.out


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestTeamMembersFetch:
    """Tests for _get_team_members error handling.

    Failures must raise: treating an API blip as "empty team" would tell
    --sync to remove every member of that team.
    """

    @patch("addteam.gh._run_checked")
    def test_raises_on_failure(self, mock_run_checked):
        mock_run_checked.side_effect = RuntimeError("HTTP 403: Must have admin rights")

        with pytest.raises(RuntimeError, match="myorg/backend-team"):
            _get_team_members("myorg", "backend-team")

    @patch("addteam.gh._run_checked")
    def test_returns_members_on_success(self, mock_run_checked):
        mock_run_checked.return_value = MagicMock(stdout="alice\nbob\ncharlie\n")

        result = _get_team_members("myorg", "backend-team")

        assert result == ["alice", "bob", "charlie"]


class TestPendingInvitationsFetch:
    """Tests for _get_pending_invitations error handling."""

    @patch("addteam.gh._run_checked")
    def test_warns_on_failure(self, mock_run_checked, capsys):
        mock_run_checked.side_effect = RuntimeError("HTTP 404: Not found")

        result = _get_pending_invitations("owner", "repo")

        assert result == set()
        captured = capsys.readouterr()
        # warnings go to stderr so stdout stays clean for piping/--json
        assert "warning" in captured.err.lower()
        assert "pending invitations" in captured.err.lower() or "admin" in captured.err.lower()


# =============================================================================
# Audit Tests
# =============================================================================


class TestAuditCollaborators:
    """Tests for _audit_collaborators drift detection."""

    def _make_config(self, collabs):
        return TeamConfig(collaborators=collabs)

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_no_drift_when_all_match(self, mock_get):
        mock_get.return_value = {"alice": "push", "bob": "admin"}
        config = self._make_config(
            [
                Collaborator("alice", "push"),
                Collaborator("bob", "admin"),
            ]
        )
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.missing == []
        assert result.extra == []
        assert result.permission_drift == []
        assert result.expired == []

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_missing_users_detected(self, mock_get):
        mock_get.return_value = {}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert len(result.missing) == 1
        assert result.missing[0].username == "alice"

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_extra_users_detected(self, mock_get):
        mock_get.return_value = {"alice": "push", "eve": "pull"}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.extra == ["eve"]

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_permission_drift_detected(self, mock_get):
        mock_get.return_value = {"alice": "pull"}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert len(result.permission_drift) == 1
        assert result.permission_drift[0] == ("alice", "pull", "push")

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_expired_users_tracked(self, mock_get):
        mock_get.return_value = {"alice": "push"}
        past = _today() - timedelta(days=1)
        config = self._make_config([Collaborator("alice", "push", expires=past)])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert len(result.expired) == 1
        assert result.expired[0].username == "alice"
        assert result.missing == []

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_case_insensitive_username_matching(self, mock_get):
        mock_get.return_value = {"Alice": "push"}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.missing == []
        assert result.extra == []

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_owner_excluded(self, mock_get):
        mock_get.return_value = {"owner": "admin", "alice": "push"}
        config = self._make_config(
            [
                Collaborator("owner", "admin"),
                Collaborator("alice", "push"),
            ]
        )
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.missing == []
        # owner should not appear in extra either
        assert "owner" not in result.extra

    @patch("addteam.app._get_collaborators_with_permissions")
    def test_authenticated_user_excluded(self, mock_get):
        mock_get.return_value = {"me": "admin", "alice": "push"}
        config = self._make_config(
            [
                Collaborator("me", "admin"),
                Collaborator("alice", "push"),
            ]
        )
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.missing == []
        assert "me" not in result.extra


# =============================================================================
# Permission Mapping Tests
# =============================================================================


class TestGetCollaboratorsPermissions:
    """Tests for _get_collaborators_with_permissions mapping."""

    def _mock_result(self, items):
        import json

        m = MagicMock()
        m.stdout = json.dumps(items)
        return m

    @patch("addteam.gh._run_checked")
    def test_read_maps_to_pull(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "read"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "pull"

    @patch("addteam.gh._run_checked")
    def test_write_maps_to_push(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "write"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "push"

    @patch("addteam.gh._run_checked")
    def test_maintain_unchanged(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "maintain"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "maintain"

    @patch("addteam.gh._run_checked")
    def test_admin_unchanged(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "admin"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "admin"

    @patch("addteam.gh._run_checked")
    def test_empty_response(self, mock_run):
        mock_run.return_value = self._mock_result([])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result == {}


# =============================================================================
# Handle Apply Tests
# =============================================================================


def _make_args(**overrides):
    """Build a minimal argparse.Namespace for _handle_apply."""
    defaults = {
        "dry_run": False,
        "sync": False,
        "quiet": True,
        "no_ai": True,
        "no_welcome": True,
        "provider": "auto",
        "json": False,
        "yes": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestHandleApply:
    """Tests for _handle_apply invite/skip/fail flow."""

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_successful_invite(self, mock_run, mock_collabs, mock_pending):
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_called_once()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push"})
    @patch("addteam.app._run")
    def test_skip_already_has_access(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value={"alice"})
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_skip_already_invited(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_skip_expired(self, mock_run, mock_collabs, mock_pending):
        past = _today() - timedelta(days=1)
        config = TeamConfig(collaborators=[Collaborator("alice", "push", expires=past)])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_skip_owner(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("owner", "admin")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_dry_run_no_api_calls(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(dry_run=True), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_failed_invite_returns_exit_code_1(self, mock_run, mock_collabs, mock_pending):
        mock_run.return_value = MagicMock(returncode=1, stderr="forbidden", stdout="")
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 1


# =============================================================================
# AI Provider Tests
# =============================================================================


class TestGenerateRepoSummary:
    """Tests for _generate_repo_summary after provider dict refactor."""

    @patch("addteam.ai._http_post_json")
    def test_responses_format_dispatches(self, mock_post, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        mock_post.return_value = {
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "summary text"}]},
            ],
        }
        result = _generate_repo_summary(
            provider="openai",
            repo_full_name="owner/repo",
            repo_description="desc",
        )
        assert result == "summary text"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "openai.com" in call_url
        assert "/responses" in call_url

    @patch("addteam.ai._http_post_json")
    def test_anthropic_format_dispatches(self, mock_post, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        mock_post.return_value = {"content": [{"text": "anthropic summary"}]}
        result = _generate_repo_summary(
            provider="anthropic",
            repo_full_name="owner/repo",
            repo_description="desc",
        )
        assert result == "anthropic summary"
        call_headers = mock_post.call_args[1]["headers"]
        assert "x-api-key" in call_headers

    @patch("addteam.ai._http_post_json")
    def test_google_format_dispatches(self, mock_post, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
        mock_post.return_value = {"candidates": [{"content": {"parts": [{"text": "google summary"}]}}]}
        result = _generate_repo_summary(
            provider="google",
            repo_full_name="owner/repo",
            repo_description="desc",
        )
        assert result == "google summary"
        call_url = mock_post.call_args[0][0]
        assert "generativelanguage" in call_url
        assert "key=test-key" in call_url

    def test_unknown_provider_raises(self):
        with pytest.raises(RuntimeError, match="Unknown provider"):
            _generate_repo_summary(
                provider="invalid",
                repo_full_name="owner/repo",
                repo_description="desc",
            )

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            _generate_repo_summary(
                provider="openai",
                repo_full_name="owner/repo",
                repo_description="desc",
            )


# =============================================================================
# Sync Removal Path (--sync in _handle_apply)
# =============================================================================


class TestSyncRemoval:
    """Tests for the --sync removal path — the most dangerous code path (gh api -X DELETE)."""

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch(
        "addteam.app._get_collaborators_with_permissions",
        return_value={"alice": "push", "eve": "pull"},
    )
    @patch("addteam.app._run")
    def test_sync_removes_extra_users(self, mock_run, mock_collabs, mock_pending):
        """Users not in config are removed via DELETE."""
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 0
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1
        assert "collaborators/eve" in delete_calls[0][0][0][-1]

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch(
        "addteam.app._get_collaborators_with_permissions",
        return_value={"alice": "push"},
    )
    @patch("addteam.app._run")
    def test_sync_preserves_configured_users(self, mock_run, mock_collabs, mock_pending):
        """Users present in config are never removed."""
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 0
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 0

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions")
    @patch("addteam.app._run")
    def test_sync_removes_expired_users(self, mock_run, mock_collabs, mock_pending):
        """Expired users who still have access are removed."""
        mock_collabs.return_value = {"alice": "push"}
        mock_run.return_value = MagicMock(returncode=0)
        past = _today() - timedelta(days=1)
        config = TeamConfig(collaborators=[Collaborator("alice", "push", expires=past)])
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 0
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1
        assert "collaborators/alice" in delete_calls[0][0][0][-1]

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch(
        "addteam.app._get_collaborators_with_permissions",
        return_value={"alice": "push", "eve": "pull"},
    )
    @patch("addteam.app._run")
    def test_sync_dry_run_never_deletes(self, mock_run, mock_collabs, mock_pending):
        """Dry-run mode previews removals but makes no DELETE calls."""
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True, dry_run=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch(
        "addteam.app._get_collaborators_with_permissions",
        return_value={"owner": "admin", "me": "push", "alice": "push", "eve": "pull"},
    )
    @patch("addteam.app._run")
    def test_sync_never_removes_owner_or_self(self, mock_run, mock_collabs, mock_pending):
        """The repo owner and authenticated user are always protected from removal."""
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 0
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        removed_users = [c[0][0][-1].split("/")[-1] for c in delete_calls]
        assert "owner" not in removed_users
        assert "me" not in removed_users
        assert "eve" in removed_users
        assert len(delete_calls) == 1

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions")
    @patch("addteam.app._run")
    def test_sync_returns_1_on_collaborator_fetch_error(self, mock_run, mock_collabs, mock_pending):
        """Returns exit code 1 if collaborator list can't be fetched during sync."""
        # First call succeeds (invite phase), second call fails (sync phase)
        mock_collabs.side_effect = [{"alice": "push"}, RuntimeError("API error")]
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 1

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch(
        "addteam.app._get_collaborators_with_permissions",
        return_value={"Alice": "push", "eve": "pull"},
    )
    @patch("addteam.app._run")
    def test_sync_case_insensitive_matching(self, mock_run, mock_collabs, mock_pending):
        """Sync uses case-insensitive comparison so 'Alice' matches config 'alice'."""
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 0
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        removed_users = [c[0][0][-1].split("/")[-1] for c in delete_calls]
        # Alice (different case) should NOT be removed — she matches config
        assert "Alice" not in removed_users
        # eve should be removed
        assert "eve" in removed_users


# =============================================================================
# _resolve_team_config (cascading config resolution)
# =============================================================================


class TestResolveTeamConfig:
    """Tests for the cascading config resolution logic."""

    @patch("addteam.config._load_team_config")
    @patch("addteam.config._resolve_local_path")
    def test_auto_resolve_finds_local_file(self, mock_resolve, mock_load):
        """Auto-resolve finds team.yaml on the local filesystem."""
        expected = TeamConfig(collaborators=[Collaborator("alice", "push")])
        mock_resolve.return_value = Path("/tmp/team.yaml")
        mock_load.return_value = expected

        config, source = _resolve_team_config("team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"
        assert "local" in source.lower() or "/tmp/team.yaml" in source

    @patch("addteam.config._gh_read_repo_file")
    @patch("addteam.config._resolve_local_path", return_value=None)
    def test_auto_resolve_falls_back_to_repo(self, mock_resolve, mock_gh_read):
        """Falls back to reading from the target repo when no local file exists."""
        mock_gh_read.return_value = "developers:\n  - alice\n"

        config, _source = _resolve_team_config("team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"

    @patch("addteam.config._gh_read_repo_file")
    def test_remote_repo_reference(self, mock_gh_read):
        """owner/repo format fetches config from a remote repo."""
        mock_gh_read.return_value = "developers:\n  - alice\n"

        config, _source = _resolve_team_config("other-org/team-configs", "owner", "repo")
        assert config.collaborators[0].username == "alice"
        mock_gh_read.assert_called_with("other-org", "team-configs", "team.yaml")

    @patch("addteam.config._gh_read_repo_file")
    def test_remote_repo_tries_yml_fallback(self, mock_gh_read):
        """Falls back to team.yml when team.yaml is not found in remote repo."""
        mock_gh_read.side_effect = [
            RuntimeError("HTTP 404: Not found"),
            "developers:\n  - alice\n",
        ]

        config, _source = _resolve_team_config("other-org/configs", "owner", "repo")
        assert config.collaborators[0].username == "alice"

    @patch("addteam.config._gh_read_repo_file")
    def test_repo_prefix_reads_from_target(self, mock_gh_read):
        """repo: prefix reads from the target repo."""
        mock_gh_read.return_value = "developers:\n  - alice\n"

        config, _source = _resolve_team_config("repo:team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"
        mock_gh_read.assert_called_with("owner", "repo", "team.yaml")

    @patch("addteam.config._load_team_config")
    @patch("addteam.config._resolve_local_path")
    def test_local_prefix_reads_local_file(self, mock_resolve, mock_load):
        """local: prefix reads from the local filesystem."""
        expected = TeamConfig(collaborators=[Collaborator("alice", "push")])
        mock_resolve.return_value = Path("/tmp/team.yaml")
        mock_load.return_value = expected

        config, _source = _resolve_team_config("local:team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"

    @patch("addteam.config._gh_read_repo_file")
    @patch("addteam.config._resolve_local_path", return_value=None)
    def test_not_found_raises_file_not_found(self, mock_resolve, mock_gh_read):
        """Raises FileNotFoundError when no config found anywhere."""
        mock_gh_read.side_effect = RuntimeError("HTTP 404: Not found")

        with pytest.raises(FileNotFoundError):
            _resolve_team_config("team.yaml", "owner", "repo")

    @patch("addteam.config._resolve_local_path", return_value=None)
    def test_explicit_local_path_no_repo_fallback(self, mock_resolve):
        """Explicit local paths (./file) don't fall back to repo."""
        with pytest.raises(FileNotFoundError):
            _resolve_team_config("./team.yaml", "owner", "repo")


# =============================================================================
# _create_welcome_issue
# =============================================================================


class TestCreateWelcomeIssue:
    """Tests for welcome issue creation and body assembly."""

    def _repo_info(self, **overrides):
        defaults = {
            "description": "A test repo",
            "homepage": "",
            "language": "",
            "html_url": "https://github.com/owner/repo",
            "topics": [],
        }
        defaults.update(overrides)
        return defaults

    def _get_body(self, mock_run_checked):
        """Extract the --body argument from the gh issue create call."""
        cmd = mock_run_checked.call_args[0][0]
        return cmd[cmd.index("--body") + 1]

    @patch("addteam.gh._run_checked")
    @patch("addteam.gh._get_repo_info")
    def test_creates_issue_with_ai_summary(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info()
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        url = _create_welcome_issue("owner", "repo", "alice", "AI generated summary", "push")
        assert url == "https://github.com/owner/repo/issues/1"
        body = self._get_body(mock_run)
        assert "AI generated summary" in body
        assert "@alice" in body

    @patch("addteam.gh._run_checked")
    @patch("addteam.gh._get_repo_info")
    def test_falls_back_to_description_without_summary(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(description="A great tool")
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        url = _create_welcome_issue("owner", "repo", "alice", None, "push")
        assert url is not None
        body = self._get_body(mock_run)
        assert "A great tool" in body

    @patch("addteam.gh._run_checked")
    @patch("addteam.gh._get_repo_info")
    def test_includes_python_language_hints(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(language="Python")
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        _create_welcome_issue("owner", "repo", "alice", None, "push")
        body = self._get_body(mock_run)
        assert "pip" in body.lower() or "python" in body.lower()

    @patch("addteam.gh._run_checked")
    @patch("addteam.gh._get_repo_info")
    def test_includes_topics(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(topics=["python", "cli"])
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        _create_welcome_issue("owner", "repo", "alice", None, "push")
        body = self._get_body(mock_run)
        assert "python" in body
        assert "cli" in body

    @patch("addteam.gh._run_checked")
    @patch("addteam.gh._get_repo_info")
    def test_includes_homepage_link(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(homepage="https://example.com")
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        _create_welcome_issue("owner", "repo", "alice", None, "push")
        body = self._get_body(mock_run)
        assert "https://example.com" in body

    @patch("addteam.gh._run_checked")
    @patch("addteam.gh._get_repo_info")
    def test_returns_none_on_api_failure(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info()
        mock_run.side_effect = RuntimeError("HTTP 403: Must have admin rights")

        result = _create_welcome_issue("owner", "repo", "alice", None, "push")
        assert result is None


# =============================================================================
# _handle_init
# =============================================================================


class TestHandleInit:
    """Direct tests for _handle_init (file creation logic)."""

    def _init_args(self, **overrides):
        defaults = {"init": False, "init_action": False, "init_multi_repo": False}
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    @patch("addteam.app._gh_json")
    def test_init_creates_team_yaml_with_repo_info(self, mock_gh_json, tmp_path, monkeypatch):
        """Uses gh repo view to populate template with real repo name/owner."""
        mock_gh_json.return_value = {"name": "my-project", "owner": {"login": "myorg"}}
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init=True))
        assert result == 0
        content = (tmp_path / "team.yaml").read_text()
        assert "my-project" in content
        assert "myorg" in content

    @patch("addteam.app._gh_json")
    def test_init_falls_back_to_defaults_outside_repo(self, mock_gh_json, tmp_path, monkeypatch):
        """Falls back to placeholder names when not inside a git repo."""
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init=True))
        assert result == 0
        content = (tmp_path / "team.yaml").read_text()
        assert "my-repo" in content
        assert "your-username" in content

    @patch("addteam.app._gh_json")
    def test_init_skips_existing_team_yaml(self, mock_gh_json, tmp_path, monkeypatch):
        """Does not overwrite an existing team.yaml."""
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "team.yaml").write_text("existing content")

        result = _handle_init(self._init_args(init=True))
        assert result == 0
        assert (tmp_path / "team.yaml").read_text() == "existing content"

    @patch("addteam.app._gh_json")
    def test_init_action_creates_single_repo_workflow(self, mock_gh_json, tmp_path, monkeypatch):
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init_action=True))
        assert result == 0
        workflows = list((tmp_path / ".github" / "workflows").glob("*.yml"))
        assert len(workflows) == 1

    @patch("addteam.app._gh_json")
    def test_init_multi_repo_creates_workflow_and_repos_txt(self, mock_gh_json, tmp_path, monkeypatch):
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init_multi_repo=True))
        assert result == 0
        workflows = list((tmp_path / ".github" / "workflows").glob("*.yml"))
        assert len(workflows) == 1
        assert (tmp_path / "repos.txt").exists()

    @patch("addteam.app._gh_json")
    def test_init_multi_repo_skips_existing_repos_txt(self, mock_gh_json, tmp_path, monkeypatch):
        """Does not overwrite an existing repos.txt."""
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "repos.txt").write_text("owner/existing\n")

        result = _handle_init(self._init_args(init_multi_repo=True))
        assert result == 0
        assert (tmp_path / "repos.txt").read_text() == "owner/existing\n"


# =============================================================================
# _handle_audit
# =============================================================================


class TestHandleAudit:
    """Direct tests for _handle_audit output and return values."""

    @patch("addteam.app._audit_collaborators")
    def test_no_drift_returns_zero(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult()
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "no drift" in captured.out.lower()

    @patch("addteam.app._audit_collaborators")
    def test_missing_users_shown(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "bob" in captured.out

    @patch("addteam.app._audit_collaborators")
    def test_extra_users_shown(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(extra=["eve"])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "eve" in captured.out

    @patch("addteam.app._audit_collaborators")
    def test_permission_drift_shown(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(permission_drift=[("alice", "pull", "push")])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "alice" in captured.out
        assert "pull" in captured.out
        assert "push" in captured.out

    @patch("addteam.app._audit_collaborators")
    def test_expired_users_shown(self, mock_audit, capsys):
        past = _today() - timedelta(days=1)
        mock_audit.return_value = AuditResult(expired=[Collaborator("temp", "push", expires=past)])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "temp" in captured.out

    @patch("addteam.app._audit_collaborators")
    def test_shows_total_drift_count(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(
            missing=[Collaborator("bob", "push")],
            extra=["eve"],
        )
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "2" in captured.out

    @patch("addteam.app._audit_collaborators")
    def test_drift_still_returns_zero(self, mock_audit):
        """Audit mode is informational — always returns 0."""
        mock_audit.return_value = AuditResult(
            missing=[Collaborator("bob", "push")],
            extra=["eve"],
            permission_drift=[("alice", "pull", "push")],
        )
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0


# =============================================================================
# Regression tests: config resolution vs repo-spec ambiguity
# =============================================================================


class TestPathVsRepoAmbiguity:
    """A relative path containing '/' must be a local file, not owner/repo."""

    def test_nested_relative_path_resolves_as_local_file(self, tmp_path, monkeypatch):
        (tmp_path / "examples").mkdir()
        (tmp_path / "examples" / "team.yaml").write_text("developers:\n  - alice\n")
        monkeypatch.chdir(tmp_path)

        config, source = _resolve_team_config("examples/team.yaml", "owner", "repo")

        assert config.collaborators[0].username == "alice"
        assert source.startswith("local:")

    @patch("addteam.config._gh_read_repo_file")
    def test_nonexistent_nested_path_still_tries_remote(self, mock_gh_read, tmp_path, monkeypatch):
        mock_gh_read.return_value = "developers:\n  - alice\n"
        monkeypatch.chdir(tmp_path)

        config, _source = _resolve_team_config("other-org/team-configs", "owner", "repo")

        assert config.collaborators[0].username == "alice"
        mock_gh_read.assert_called_with("other-org", "team-configs", "team.yaml")

    @patch("addteam.app.shutil.which")
    @patch("addteam.config._gh_read_repo_file")
    @patch("addteam.app._gh_json")
    @patch("addteam.app._gh_text")
    def test_missing_config_exits_1(self, mock_text, mock_json, mock_read, mock_which, tmp_path, monkeypatch, capsys):
        """A missing config is a failure for automation (was exit 0)."""
        mock_which.return_value = "/usr/bin/gh"
        mock_json.return_value = {"name": "repo", "owner": {"login": "owner"}, "description": ""}
        mock_text.return_value = "me"
        mock_read.side_effect = RuntimeError("HTTP 404: Not Found")
        monkeypatch.chdir(tmp_path)

        result = run([])

        assert result == 1
        captured = capsys.readouterr()
        assert "no team config" in captured.err.lower()
        assert "--init" in captured.err


# =============================================================================
# TeamConfig parsing: contractors role, unknown keys, team failures
# =============================================================================


class TestConfigWarnings:
    def test_contractors_role_supported(self):
        yaml = """
contractors:
  - username: temp-dev
    permission: push
    expires: 2030-06-01
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert len(config.collaborators) == 1
        assert config.collaborators[0].username == "temp-dev"
        assert config.collaborators[0].expires == date(2030, 6, 1)

    def test_unknown_key_produces_warning_and_drops_nothing_else(self):
        yaml = """
develoeprs:
  - alice
admins:
  - root
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert [c.username for c in config.collaborators] == ["root"]
        assert any("develoeprs" in w for w in config.warnings)
        assert not config.incomplete

    def test_example_file_parses_fully(self):
        repo_root = Path(__file__).resolve().parents[1]
        content = (repo_root / "examples" / "team.yaml").read_text()
        config = _parse_yaml_config(content, "owner", "repo")
        usernames = {c.username for c in config.collaborators}
        assert {
            "repo-owner",
            "lead-dev",
            "alice",
            "bob",
            "charlie",
            "eve",
            "external-consultant",
            "intern-jane",
        } <= usernames
        assert config.warnings == []

    @patch("addteam.config._get_team_members", side_effect=RuntimeError("could not fetch team myorg/devs: 403"))
    def test_team_failure_marks_config_incomplete(self, _mock_members):
        yaml = """
teams:
  - myorg/devs
"""
        config = _parse_yaml_config(yaml, "owner", "repo")
        assert config.incomplete is True
        assert any("myorg/devs" in w for w in config.warnings)


class TestSyncIncompleteConfig:
    """--sync must refuse a partially-resolved config (mass-removal guard)."""

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"eve": "pull"})
    @patch("addteam.app._run")
    def test_sync_refused_when_config_incomplete(self, mock_run, mock_collabs, mock_pending, capsys):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")], incomplete=True)
        result = _handle_apply(
            _make_args(sync=True),
            config,
            "owner",
            "repo",
            "owner/repo",
            "",
            "me",
        )
        assert result == 1
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert delete_calls == []
        captured = capsys.readouterr()
        assert "refusing to" in captured.err


# =============================================================================
# Permission drift convergence in apply mode
# =============================================================================


class TestPermissionDriftConvergence:
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "pull"})
    @patch("addteam.app._run")
    def test_existing_user_wrong_permission_gets_updated(self, mock_run, mock_collabs, mock_pending):
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 0
        put_calls = [c for c in mock_run.call_args_list if "PUT" in c[0][0]]
        assert len(put_calls) == 1
        put_cmd = " ".join(put_calls[0][0][0])
        assert "collaborators/alice" in put_cmd
        assert "permission=push" in put_cmd

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "pull"})
    @patch("addteam.app._run")
    def test_drift_update_previewed_in_dry_run(self, mock_run, mock_collabs, mock_pending, capsys):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_apply(_make_args(dry_run=True, quiet=False), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 0
        mock_run.assert_not_called()
        captured = capsys.readouterr()
        assert "pull → push" in captured.out
        assert "would update" in captured.out

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push"})
    @patch("addteam.app._run")
    def test_matching_permission_still_skips(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch(
        "addteam.app._get_collaborators_with_permissions",
        return_value={
            "alice": "pull",
            "carol": "maintain",
        },
    )
    @patch("addteam.app._run")
    def test_failed_update_counts_toward_exit_code(self, mock_run, mock_collabs, mock_pending):
        mock_run.return_value = MagicMock(returncode=1, stderr="forbidden", stdout="")
        config = TeamConfig(
            collaborators=[
                Collaborator("alice", "push"),
                Collaborator("carol", "maintain"),  # matches -> skipped, no call
            ]
        )
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 1
        assert mock_run.call_count == 1  # only the failed update


# =============================================================================
# Terminal rendering (markup-escape regression)
# =============================================================================


class TestApplyOutputRendering:
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_permission_visible_in_dry_run_output(self, mock_run, mock_collabs, mock_pending, capsys):
        """Bug: 'invite [push]' was parsed as Rich markup and rendered blank."""
        config = TeamConfig(collaborators=[Collaborator("alice", "admin")])

        result = _handle_apply(_make_args(dry_run=True, quiet=False), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 0
        captured = capsys.readouterr()
        assert "alice" in captured.out
        assert "admin" in captured.out  # permission must actually render


# =============================================================================
# Audit mode: --fail-on-drift, --json, pending-invite annotation
# =============================================================================


def _audit_args(**overrides):
    defaults = {"json": False, "fail_on_drift": False, "quiet": False}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestAuditEnhancements:
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._audit_collaborators")
    def test_fail_on_drift_returns_1(self, mock_audit, mock_pending):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args(fail_on_drift=True))
        assert result == 1

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._audit_collaborators")
    def test_no_drift_with_fail_on_drift_returns_0(self, mock_audit, mock_pending):
        mock_audit.return_value = AuditResult()
        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args(fail_on_drift=True))
        assert result == 0

    @patch("addteam.app._get_pending_invitations", return_value={"bob"})
    @patch("addteam.app._audit_collaborators")
    def test_pending_invite_annotated(self, mock_audit, mock_pending, capsys):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args())
        assert result == 0
        captured = capsys.readouterr()
        assert "invite pending" in captured.out

    @patch("addteam.app._get_pending_invitations", return_value={"bob"})
    @patch("addteam.app._audit_collaborators")
    def test_json_output_payload(self, mock_audit, mock_pending, capsys):
        mock_audit.return_value = AuditResult(
            missing=[Collaborator("bob", "push")],
            extra=["eve"],
            permission_drift=[("alice", "pull", "push")],
        )
        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args(json=True, fail_on_drift=True))

        assert result == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "audit"
        assert payload["repo"] == "owner/repo"
        assert payload["drift"] == 3
        assert payload["missing"][0]["username"] == "bob"
        assert payload["missing"][0]["invite_pending"] is True
        assert payload["extra"] == ["eve"]
        assert payload["permission_drift"][0] == {"username": "alice", "current": "pull", "expected": "push"}


# =============================================================================
# Apply mode: --json output
# =============================================================================


class TestApplyJsonOutput:
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._run")
    def test_apply_json_payload(self, mock_run, mock_collabs, mock_pending, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_apply(_make_args(json=True), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "apply"
        assert payload["repo"] == "owner/repo"
        assert payload["summary"]["invited"] == 1
        assert payload["results"][0]["username"] == "alice"
        assert payload["results"][0]["status"] == "ok"

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push", "eve": "pull"})
    @patch("addteam.app._run")
    def test_sync_json_payload_includes_removals(self, mock_run, mock_collabs, mock_pending, capsys):
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_apply(_make_args(json=True, sync=True), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["removed"] == 1
        assert payload["removals"] == [{"username": "eve", "status": "removed"}]


# =============================================================================
# Sync removal confirmation
# =============================================================================


class TestRemovalConfirmation:
    @patch("addteam.app.confirm_removals", return_value=False)
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push", "eve": "pull"})
    @patch("addteam.app._run")
    def test_declined_confirmation_skips_removals(self, mock_run, mock_collabs, mock_pending, mock_confirm, capsys):
        with (
            patch("sys.stdin.isatty", return_value=True),
            patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True),
        ):
            config = TeamConfig(collaborators=[Collaborator("alice", "push")])
            result = _handle_apply(_make_args(sync=True, quiet=False), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 0
        mock_confirm.assert_called_once()
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert delete_calls == []

    @patch("addteam.app.confirm_removals")
    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push", "eve": "pull"})
    @patch("addteam.app._run")
    def test_yes_flag_skips_prompt(self, mock_run, mock_collabs, mock_pending, mock_confirm):
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_apply(
            _make_args(sync=True, quiet=False, yes=True), config, "owner", "repo", "owner/repo", "", "me"
        )

        assert result == 0
        mock_confirm.assert_not_called()
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1

    @patch("addteam.app._get_pending_invitations", return_value=set())
    @patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push", "eve": "pull"})
    @patch("addteam.app._run")
    def test_remove_failure_counts_toward_exit_code(self, mock_run, mock_collabs, mock_pending):
        mock_run.return_value = MagicMock(returncode=1, stderr="forbidden", stdout="")
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_apply(_make_args(sync=True), config, "owner", "repo", "owner/repo", "", "me")

        assert result == 1
        delete_calls = [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1


# =============================================================================
# Welcome-issue resolution (flags vs config vs default)
# =============================================================================


class TestWelcomeResolution:
    def _args(self, welcome=None, no_welcome=False):
        return argparse.Namespace(welcome=welcome, no_welcome=no_welcome)

    def test_default_is_on(self):
        assert _resolve_welcome(self._args(), TeamConfig(welcome_issue=None)) is True

    def test_config_false_respected(self):
        assert _resolve_welcome(self._args(), TeamConfig(welcome_issue=False)) is False

    def test_config_true_respected(self):
        assert _resolve_welcome(self._args(), TeamConfig(welcome_issue=True)) is True

    def test_no_welcome_flag_overrides_config_true(self):
        assert _resolve_welcome(self._args(no_welcome=True), TeamConfig(welcome_issue=True)) is False

    def test_welcome_flag_overrides_config_false(self):
        assert _resolve_welcome(self._args(welcome=True), TeamConfig(welcome_issue=False)) is True

    def test_no_welcome_beats_welcome_flag(self):
        # defensive: both flags passed -> off wins
        assert _resolve_welcome(self._args(welcome=True, no_welcome=True), TeamConfig()) is False


# =============================================================================
# CLI validation
# =============================================================================


class TestCliValidation:
    def test_conflicting_config_sources_rejected(self, capsys):
        result = run(["--from", "org/configs", "-f", "team.yaml"])
        assert result == 2
        captured = capsys.readouterr()
        assert "conflicting" in captured.err.lower()

    def test_invalid_from_repo_rejected(self, capsys):
        result = run(["--from", "not-a-repo"])
        assert result == 2

    def test_positional_and_from_conflict(self):
        result = run(["other-org/configs", "--from", "org/configs"])
        assert result == 2


# =============================================================================
# Update check (cached, CI-aware)
# =============================================================================


class TestUpdateCheck:
    def _seed_cache(self, tmp_path, latest, checked_at=None):
        cache_dir = tmp_path / "addteam"
        cache_dir.mkdir(parents=True)
        (cache_dir / "update-check.json").write_text(
            json.dumps({"checked_at": checked_at if checked_at is not None else time.time(), "latest": latest})
        )

    @patch("addteam.ui.httpx.get")
    def test_skips_in_ci(self, mock_get, monkeypatch):
        monkeypatch.setenv("CI", "true")
        from addteam.ui import check_for_updates

        check_for_updates()
        mock_get.assert_not_called()

    @patch("addteam.ui.httpx.get")
    def test_skips_when_opted_out(self, mock_get, monkeypatch):
        monkeypatch.setenv("ADDTEAM_NO_UPDATE_CHECK", "1")
        from addteam.ui import check_for_updates

        check_for_updates()
        mock_get.assert_not_called()

    @patch("addteam.ui.httpx.get")
    def test_uses_fresh_cache_without_network(self, mock_get, monkeypatch, tmp_path, capsys):
        from addteam.ui import check_for_updates

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("ADDTEAM_NO_UPDATE_CHECK", raising=False)
        self._seed_cache(tmp_path, "99.0.0")

        with patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True):
            check_for_updates()

        mock_get.assert_not_called()
        captured = capsys.readouterr()
        assert "update available" in captured.err
        assert "99.0.0" in captured.err

    @patch("addteam.ui.httpx.get")
    def test_no_notice_when_up_to_date(self, mock_get, monkeypatch, tmp_path, capsys):
        from addteam.ui import check_for_updates

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("ADDTEAM_NO_UPDATE_CHECK", raising=False)
        self._seed_cache(tmp_path, "0.0.1")

        with patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True):
            check_for_updates()

        captured = capsys.readouterr()
        assert "update available" not in captured.err

    @patch("addteam.ui.httpx.get")
    def test_stale_cache_refetches_and_writes(self, mock_get, monkeypatch, tmp_path):
        from addteam.ui import check_for_updates

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("ADDTEAM_NO_UPDATE_CHECK", raising=False)
        self._seed_cache(tmp_path, "99.0.0", checked_at=time.time() - 90000)

        response = MagicMock(status_code=200)
        response.json.return_value = {"info": {"version": "99.1.0"}}
        mock_get.return_value = response

        with patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True):
            check_for_updates()

        mock_get.assert_called_once()
        cached = json.loads((tmp_path / "addteam" / "update-check.json").read_text())
        assert cached["latest"] == "99.1.0"

    @patch("addteam.ui.httpx.get", side_effect=httpx.ConnectError("offline"))
    def test_network_failure_is_silent(self, mock_get, monkeypatch, tmp_path, capsys):
        from addteam.ui import check_for_updates

        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("ADDTEAM_NO_UPDATE_CHECK", raising=False)

        with patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True):
            check_for_updates()  # must not raise

        captured = capsys.readouterr()
        assert "update available" not in captured.err


# =============================================================================
# Backwards-compat import shim
# =============================================================================


class TestCompatShim:
    def test_bootstrap_repo_still_exports_run(self):
        import addteam.bootstrap_repo as shim

        assert callable(shim.run)
        assert shim.Collaborator is Collaborator
        assert shim.TeamConfig is TeamConfig
        assert shim.__version__

    def test_version_matches_package_metadata(self):
        from importlib.metadata import version

        import addteam

        assert addteam.__version__ == version("addteam")
