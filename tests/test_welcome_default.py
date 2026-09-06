"""v1.6.0: welcome issues are opt-in, not opt-out."""

import argparse
from unittest.mock import MagicMock, patch

from conftest import _make_args, _make_repo_json, _run_mocks

from addteam.app import _handle_apply, _resolve_welcome, run
from addteam.models import Collaborator, TeamConfig


def test_no_config_no_flags_defaults_off():
    args = argparse.Namespace(welcome=None, no_welcome=False)
    assert _resolve_welcome(args, TeamConfig(welcome_issue=None)) is False


@patch("addteam.app._create_welcome_issue")
@patch("addteam.app._get_pending_invitations", return_value={})
@patch("addteam.app._get_collaborators_with_permissions", return_value={})
@patch("addteam.app._run", return_value=MagicMock(returncode=0))
def test_apply_creates_no_welcome_issue_by_default(mock_run, mock_collabs, mock_pending, mock_welcome):
    # welcome_issue left unset (None) at construction, then explicitly resolved
    # to False — what run() would set via _resolve_welcome.
    config = TeamConfig(collaborators=[Collaborator("alice", "push")])
    config.welcome_issue = False
    result = _handle_apply(_make_args(), config, "owner", "repo", "owner/repo", "", "me")
    assert result == 0
    mock_welcome.assert_not_called()


def test_end_to_end_run_no_welcome_by_default(tmp_path, monkeypatch):
    mocks = _run_mocks() + [
        patch("addteam.app._run"),
        patch("addteam.app._create_welcome_issue"),
        patch("addteam.app._generate_repo_summary"),
    ]
    with (
        mocks[0],
        mocks[1] as mock_json,
        mocks[2] as mock_text,
        mocks[3],
        mocks[4],
        mocks[5] as mock_run,
        mocks[6] as mock_welcome,
        mocks[7] as mock_summary,
    ):
        mock_json.return_value = _make_repo_json()
        mock_text.return_value = "me"
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        (tmp_path / "team.yaml").write_text("developers:\n  - alice\n")
        monkeypatch.chdir(tmp_path)

        result = run([])

    assert result == 0
    mock_welcome.assert_not_called()
    mock_summary.assert_not_called()


def test_config_opt_in_still_works(tmp_path, monkeypatch):
    mocks = _run_mocks() + [
        patch("addteam.app._run"),
        patch("addteam.app._create_welcome_issue"),
        patch("addteam.app.available_providers", return_value=[]),
    ]
    with (
        mocks[0],
        mocks[1] as mock_json,
        mocks[2] as mock_text,
        mocks[3],
        mocks[4],
        mocks[5] as mock_run,
        mocks[6] as mock_welcome,
        mocks[7],
    ):
        mock_json.return_value = _make_repo_json()
        mock_text.return_value = "me"
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        (tmp_path / "team.yaml").write_text("welcome_issue: true\ndevelopers:\n  - alice\n")
        monkeypatch.chdir(tmp_path)

        result = run([])

    assert result == 0
    mock_welcome.assert_called_once()


def test_welcome_flag_opts_in_for_one_run(tmp_path, monkeypatch):
    mocks = _run_mocks() + [
        patch("addteam.app._run"),
        patch("addteam.app._create_welcome_issue"),
        patch("addteam.app.available_providers", return_value=[]),
    ]
    with (
        mocks[0],
        mocks[1] as mock_json,
        mocks[2] as mock_text,
        mocks[3],
        mocks[4],
        mocks[5] as mock_run,
        mocks[6] as mock_welcome,
        mocks[7],
    ):
        mock_json.return_value = _make_repo_json()
        mock_text.return_value = "me"
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        (tmp_path / "team.yaml").write_text("developers:\n  - alice\n")
        monkeypatch.chdir(tmp_path)

        result = run(["--welcome"])

    assert result == 0
    mock_welcome.assert_called_once()


def test_no_welcome_still_forces_off(tmp_path, monkeypatch):
    mocks = _run_mocks() + [
        patch("addteam.app._run"),
        patch("addteam.app._create_welcome_issue"),
    ]
    with (
        mocks[0],
        mocks[1] as mock_json,
        mocks[2] as mock_text,
        mocks[3],
        mocks[4],
        mocks[5] as mock_run,
        mocks[6] as mock_welcome,
    ):
        mock_json.return_value = _make_repo_json()
        mock_text.return_value = "me"
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        (tmp_path / "team.yaml").write_text("welcome_issue: true\ndevelopers:\n  - alice\n")
        monkeypatch.chdir(tmp_path)

        result = run(["--no-welcome"])

    assert result == 0
    mock_welcome.assert_not_called()
