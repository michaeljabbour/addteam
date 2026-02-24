"""Tests for addteam bootstrap_repo module."""

import argparse
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

import pytest

from pathlib import Path

from addteam.bootstrap_repo import (
    AuditResult,
    Collaborator,
    TeamConfig,
    _audit_collaborators,
    _create_welcome_issue,
    _generate_repo_summary,
    _get_collaborators_with_permissions,
    _get_pending_invitations,
    _get_team_members,
    _handle_apply,
    _handle_audit,
    _handle_init,
    _is_valid_repo_spec,
    _looks_like_local_path,
    _normalize_argv,
    _parse_date,
    _parse_usernames_txt,
    _parse_yaml_config,
    _resolve_team_config,
    run,
)


# =============================================================================
# Data Model Tests
# =============================================================================


class TestCollaborator:
    """Tests for Collaborator dataclass."""

    def test_not_expired_when_no_date(self):
        c = Collaborator(username="alice")
        assert not c.is_expired

    def test_not_expired_when_future(self):
        future = date.today() + timedelta(days=30)
        c = Collaborator(username="alice", expires=future)
        assert not c.is_expired

    def test_expired_when_past(self):
        past = date.today() - timedelta(days=1)
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
        assert config.welcome_issue is False
        assert config.welcome_message is None
        assert config.source == ""


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


class TestNormalizeArgv:
    """Tests for _normalize_argv."""

    def test_splits_combined_args(self):
        result = _normalize_argv(["--repoowner/repo"])
        assert result == ["--repo", "owner/repo"]

    def test_leaves_normal_args(self):
        result = _normalize_argv(["--repo", "owner/repo"])
        assert result == ["--repo", "owner/repo"]

    def test_handles_equals(self):
        result = _normalize_argv(["--repo=owner/repo"])
        assert result == ["--repo=owner/repo"]


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

    @patch("addteam.bootstrap_repo.shutil.which")
    def test_gh_not_found(self, mock_which, capsys):
        mock_which.return_value = None
        result = run(["owner/repo"])
        assert result == 1
        captured = capsys.readouterr()
        assert "gh" in captured.out.lower()

    @patch("addteam.bootstrap_repo.shutil.which")
    @patch("addteam.bootstrap_repo._run_checked")
    def test_init_creates_team_yaml(self, mock_run, mock_which, tmp_path, monkeypatch):
        mock_which.return_value = "/usr/bin/gh"
        mock_run.side_effect = RuntimeError("not in repo")

        monkeypatch.chdir(tmp_path)
        result = run(["--init"])

        assert result == 0
        assert (tmp_path / "team.yaml").exists()

    @patch("addteam.bootstrap_repo.shutil.which")
    @patch("addteam.bootstrap_repo._run_checked")
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

    @patch("addteam.bootstrap_repo.shutil.which")
    @patch("addteam.bootstrap_repo._gh_json")
    @patch("addteam.bootstrap_repo._gh_text")
    def test_dry_run_shows_preview(self, mock_text, mock_json, mock_which, tmp_path, monkeypatch, capsys):
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
    """Tests for _get_team_members error handling."""

    @patch("addteam.bootstrap_repo._run_checked")
    def test_warns_on_failure(self, mock_run_checked, capsys):
        mock_run_checked.side_effect = RuntimeError("HTTP 403: Must have admin rights")

        result = _get_team_members("myorg", "backend-team")

        assert result == []
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower()
        assert "myorg/backend-team" in captured.out
        assert "403" in captured.out or "admin" in captured.out.lower()

    @patch("addteam.bootstrap_repo._run_checked")
    def test_returns_members_on_success(self, mock_run_checked):
        mock_run_checked.return_value = MagicMock(stdout="alice\nbob\ncharlie\n")

        result = _get_team_members("myorg", "backend-team")

        assert result == ["alice", "bob", "charlie"]


class TestPendingInvitationsFetch:
    """Tests for _get_pending_invitations error handling."""

    @patch("addteam.bootstrap_repo._run_checked")
    def test_warns_on_failure(self, mock_run_checked, capsys):
        mock_run_checked.side_effect = RuntimeError("HTTP 404: Not found")

        result = _get_pending_invitations("owner", "repo")

        assert result == set()
        captured = capsys.readouterr()
        assert "warning" in captured.out.lower()
        assert "pending invitations" in captured.out.lower() or "admin" in captured.out.lower()


# =============================================================================
# Audit Tests
# =============================================================================


class TestAuditCollaborators:
    """Tests for _audit_collaborators drift detection."""

    def _make_config(self, collabs):
        return TeamConfig(collaborators=collabs)

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
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

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    def test_missing_users_detected(self, mock_get):
        mock_get.return_value = {}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert len(result.missing) == 1
        assert result.missing[0].username == "alice"

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    def test_extra_users_detected(self, mock_get):
        mock_get.return_value = {"alice": "push", "eve": "pull"}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.extra == ["eve"]

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    def test_permission_drift_detected(self, mock_get):
        mock_get.return_value = {"alice": "pull"}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert len(result.permission_drift) == 1
        assert result.permission_drift[0] == ("alice", "pull", "push")

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    def test_expired_users_tracked(self, mock_get):
        mock_get.return_value = {"alice": "push"}
        past = date.today() - timedelta(days=1)
        config = self._make_config([Collaborator("alice", "push", expires=past)])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert len(result.expired) == 1
        assert result.expired[0].username == "alice"
        assert result.missing == []

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    def test_case_insensitive_username_matching(self, mock_get):
        mock_get.return_value = {"Alice": "push"}
        config = self._make_config([Collaborator("alice", "push")])
        result = _audit_collaborators(config, "owner", "repo", "me")
        assert result.missing == []
        assert result.extra == []

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
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

    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
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

    @patch("addteam.bootstrap_repo._run_checked")
    def test_read_maps_to_pull(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "read"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "pull"

    @patch("addteam.bootstrap_repo._run_checked")
    def test_write_maps_to_push(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "write"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "push"

    @patch("addteam.bootstrap_repo._run_checked")
    def test_maintain_unchanged(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "maintain"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "maintain"

    @patch("addteam.bootstrap_repo._run_checked")
    def test_admin_unchanged(self, mock_run):
        mock_run.return_value = self._mock_result([{"login": "alice", "role_name": "admin"}])
        result = _get_collaborators_with_permissions("owner", "repo")
        assert result["alice"] == "admin"

    @patch("addteam.bootstrap_repo._run_checked")
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
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestHandleApply:
    """Tests for _handle_apply invite/skip/fail flow."""

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={})
    @patch("addteam.bootstrap_repo._run")
    def test_successful_invite(self, mock_run, mock_collabs, mock_pending):
        mock_run.return_value = MagicMock(returncode=0)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_called_once()

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={"alice": "push"})
    @patch("addteam.bootstrap_repo._run")
    def test_skip_already_has_access(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value={"alice"})
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={})
    @patch("addteam.bootstrap_repo._run")
    def test_skip_already_invited(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={})
    @patch("addteam.bootstrap_repo._run")
    def test_skip_expired(self, mock_run, mock_collabs, mock_pending):
        past = date.today() - timedelta(days=1)
        config = TeamConfig(collaborators=[Collaborator("alice", "push", expires=past)])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={})
    @patch("addteam.bootstrap_repo._run")
    def test_skip_owner(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("owner", "admin")])
        result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={})
    @patch("addteam.bootstrap_repo._run")
    def test_dry_run_no_api_calls(self, mock_run, mock_collabs, mock_pending):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(dry_run=True), config, "owner", "repo", "owner/repo", "", "me")
        assert result == 0
        mock_run.assert_not_called()

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions", return_value={})
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._http_post_json")
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

    @patch("addteam.bootstrap_repo._http_post_json")
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

    @patch("addteam.bootstrap_repo._http_post_json")
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch(
        "addteam.bootstrap_repo._get_collaborators_with_permissions",
        return_value={"alice": "push", "eve": "pull"},
    )
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch(
        "addteam.bootstrap_repo._get_collaborators_with_permissions",
        return_value={"alice": "push"},
    )
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    @patch("addteam.bootstrap_repo._run")
    def test_sync_removes_expired_users(self, mock_run, mock_collabs, mock_pending):
        """Expired users who still have access are removed."""
        mock_collabs.return_value = {"alice": "push"}
        mock_run.return_value = MagicMock(returncode=0)
        past = date.today() - timedelta(days=1)
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch(
        "addteam.bootstrap_repo._get_collaborators_with_permissions",
        return_value={"alice": "push", "eve": "pull"},
    )
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch(
        "addteam.bootstrap_repo._get_collaborators_with_permissions",
        return_value={"owner": "admin", "me": "push", "alice": "push", "eve": "pull"},
    )
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch("addteam.bootstrap_repo._get_collaborators_with_permissions")
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._get_pending_invitations", return_value=set())
    @patch(
        "addteam.bootstrap_repo._get_collaborators_with_permissions",
        return_value={"Alice": "push", "eve": "pull"},
    )
    @patch("addteam.bootstrap_repo._run")
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

    @patch("addteam.bootstrap_repo._load_team_config")
    @patch("addteam.bootstrap_repo._resolve_local_path")
    def test_auto_resolve_finds_local_file(self, mock_resolve, mock_load):
        """Auto-resolve finds team.yaml on the local filesystem."""
        expected = TeamConfig(collaborators=[Collaborator("alice", "push")])
        mock_resolve.return_value = Path("/tmp/team.yaml")
        mock_load.return_value = expected

        config, source = _resolve_team_config("team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"
        assert "local" in source.lower() or "/tmp/team.yaml" in source

    @patch("addteam.bootstrap_repo._gh_read_repo_file")
    @patch("addteam.bootstrap_repo._resolve_local_path", return_value=None)
    def test_auto_resolve_falls_back_to_repo(self, mock_resolve, mock_gh_read):
        """Falls back to reading from the target repo when no local file exists."""
        mock_gh_read.return_value = "developers:\n  - alice\n"

        config, source = _resolve_team_config("team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"

    @patch("addteam.bootstrap_repo._gh_read_repo_file")
    def test_remote_repo_reference(self, mock_gh_read):
        """owner/repo format fetches config from a remote repo."""
        mock_gh_read.return_value = "developers:\n  - alice\n"

        config, source = _resolve_team_config("other-org/team-configs", "owner", "repo")
        assert config.collaborators[0].username == "alice"
        mock_gh_read.assert_called_with("other-org", "team-configs", "team.yaml")

    @patch("addteam.bootstrap_repo._gh_read_repo_file")
    def test_remote_repo_tries_yml_fallback(self, mock_gh_read):
        """Falls back to team.yml when team.yaml is not found in remote repo."""
        mock_gh_read.side_effect = [
            RuntimeError("HTTP 404: Not found"),
            "developers:\n  - alice\n",
        ]

        config, source = _resolve_team_config("other-org/configs", "owner", "repo")
        assert config.collaborators[0].username == "alice"

    @patch("addteam.bootstrap_repo._gh_read_repo_file")
    def test_repo_prefix_reads_from_target(self, mock_gh_read):
        """repo: prefix reads from the target repo."""
        mock_gh_read.return_value = "developers:\n  - alice\n"

        config, source = _resolve_team_config("repo:team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"
        mock_gh_read.assert_called_with("owner", "repo", "team.yaml")

    @patch("addteam.bootstrap_repo._load_team_config")
    @patch("addteam.bootstrap_repo._resolve_local_path")
    def test_local_prefix_reads_local_file(self, mock_resolve, mock_load):
        """local: prefix reads from the local filesystem."""
        expected = TeamConfig(collaborators=[Collaborator("alice", "push")])
        mock_resolve.return_value = Path("/tmp/team.yaml")
        mock_load.return_value = expected

        config, source = _resolve_team_config("local:team.yaml", "owner", "repo")
        assert config.collaborators[0].username == "alice"

    @patch("addteam.bootstrap_repo._gh_read_repo_file")
    @patch("addteam.bootstrap_repo._resolve_local_path", return_value=None)
    def test_not_found_raises_file_not_found(self, mock_resolve, mock_gh_read):
        """Raises FileNotFoundError when no config found anywhere."""
        mock_gh_read.side_effect = RuntimeError("HTTP 404: Not found")

        with pytest.raises(FileNotFoundError):
            _resolve_team_config("team.yaml", "owner", "repo")

    @patch("addteam.bootstrap_repo._resolve_local_path", return_value=None)
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

    @patch("addteam.bootstrap_repo._run_checked")
    @patch("addteam.bootstrap_repo._get_repo_info")
    def test_creates_issue_with_ai_summary(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info()
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        url = _create_welcome_issue("owner", "repo", "alice", "AI generated summary", "push")
        assert url == "https://github.com/owner/repo/issues/1"
        body = self._get_body(mock_run)
        assert "AI generated summary" in body
        assert "@alice" in body

    @patch("addteam.bootstrap_repo._run_checked")
    @patch("addteam.bootstrap_repo._get_repo_info")
    def test_falls_back_to_description_without_summary(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(description="A great tool")
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        url = _create_welcome_issue("owner", "repo", "alice", None, "push")
        assert url is not None
        body = self._get_body(mock_run)
        assert "A great tool" in body

    @patch("addteam.bootstrap_repo._run_checked")
    @patch("addteam.bootstrap_repo._get_repo_info")
    def test_includes_python_language_hints(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(language="Python")
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        _create_welcome_issue("owner", "repo", "alice", None, "push")
        body = self._get_body(mock_run)
        assert "pip" in body.lower() or "python" in body.lower()

    @patch("addteam.bootstrap_repo._run_checked")
    @patch("addteam.bootstrap_repo._get_repo_info")
    def test_includes_topics(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(topics=["python", "cli"])
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        _create_welcome_issue("owner", "repo", "alice", None, "push")
        body = self._get_body(mock_run)
        assert "python" in body
        assert "cli" in body

    @patch("addteam.bootstrap_repo._run_checked")
    @patch("addteam.bootstrap_repo._get_repo_info")
    def test_includes_homepage_link(self, mock_info, mock_run):
        mock_info.return_value = self._repo_info(homepage="https://example.com")
        mock_run.return_value = MagicMock(stdout="https://github.com/owner/repo/issues/1\n")

        _create_welcome_issue("owner", "repo", "alice", None, "push")
        body = self._get_body(mock_run)
        assert "https://example.com" in body

    @patch("addteam.bootstrap_repo._run_checked")
    @patch("addteam.bootstrap_repo._get_repo_info")
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

    @patch("addteam.bootstrap_repo._gh_json")
    def test_init_creates_team_yaml_with_repo_info(self, mock_gh_json, tmp_path, monkeypatch):
        """Uses gh repo view to populate template with real repo name/owner."""
        mock_gh_json.return_value = {"name": "my-project", "owner": {"login": "myorg"}}
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init=True))
        assert result == 0
        content = (tmp_path / "team.yaml").read_text()
        assert "my-project" in content
        assert "myorg" in content

    @patch("addteam.bootstrap_repo._gh_json")
    def test_init_falls_back_to_defaults_outside_repo(self, mock_gh_json, tmp_path, monkeypatch):
        """Falls back to placeholder names when not inside a git repo."""
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init=True))
        assert result == 0
        content = (tmp_path / "team.yaml").read_text()
        assert "my-repo" in content
        assert "your-username" in content

    @patch("addteam.bootstrap_repo._gh_json")
    def test_init_skips_existing_team_yaml(self, mock_gh_json, tmp_path, monkeypatch):
        """Does not overwrite an existing team.yaml."""
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)
        (tmp_path / "team.yaml").write_text("existing content")

        result = _handle_init(self._init_args(init=True))
        assert result == 0
        assert (tmp_path / "team.yaml").read_text() == "existing content"

    @patch("addteam.bootstrap_repo._gh_json")
    def test_init_action_creates_single_repo_workflow(self, mock_gh_json, tmp_path, monkeypatch):
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init_action=True))
        assert result == 0
        workflows = list((tmp_path / ".github" / "workflows").glob("*.yml"))
        assert len(workflows) == 1

    @patch("addteam.bootstrap_repo._gh_json")
    def test_init_multi_repo_creates_workflow_and_repos_txt(self, mock_gh_json, tmp_path, monkeypatch):
        mock_gh_json.side_effect = RuntimeError("not in a repo")
        monkeypatch.chdir(tmp_path)

        result = _handle_init(self._init_args(init_multi_repo=True))
        assert result == 0
        workflows = list((tmp_path / ".github" / "workflows").glob("*.yml"))
        assert len(workflows) == 1
        assert (tmp_path / "repos.txt").exists()

    @patch("addteam.bootstrap_repo._gh_json")
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

    @patch("addteam.bootstrap_repo._audit_collaborators")
    def test_no_drift_returns_zero(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult()
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "no drift" in captured.out.lower()

    @patch("addteam.bootstrap_repo._audit_collaborators")
    def test_missing_users_shown(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "bob" in captured.out

    @patch("addteam.bootstrap_repo._audit_collaborators")
    def test_extra_users_shown(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(extra=["eve"])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "eve" in captured.out

    @patch("addteam.bootstrap_repo._audit_collaborators")
    def test_permission_drift_shown(self, mock_audit, capsys):
        mock_audit.return_value = AuditResult(permission_drift=[("alice", "pull", "push")])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "alice" in captured.out
        assert "pull" in captured.out
        assert "push" in captured.out

    @patch("addteam.bootstrap_repo._audit_collaborators")
    def test_expired_users_shown(self, mock_audit, capsys):
        past = date.today() - timedelta(days=1)
        mock_audit.return_value = AuditResult(expired=[Collaborator("temp", "push", expires=past)])
        config = TeamConfig(collaborators=[])

        result = _handle_audit(config, "owner", "repo", "me")
        assert result == 0
        captured = capsys.readouterr()
        assert "temp" in captured.out

    @patch("addteam.bootstrap_repo._audit_collaborators")
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

    @patch("addteam.bootstrap_repo._audit_collaborators")
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
