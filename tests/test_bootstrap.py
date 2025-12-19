"""Tests for addteam bootstrap_repo module."""

from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from addteam.bootstrap_repo import (
    Collaborator,
    TeamConfig,
    AuditResult,
    _parse_usernames_txt,
    _parse_date,
    _parse_yaml_config,
    _is_valid_repo_spec,
    _looks_like_local_path,
    _normalize_argv,
    _get_team_members,
    _get_pending_invitations,
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
