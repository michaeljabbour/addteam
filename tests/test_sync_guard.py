"""v1.6.0: --sync circuit breaker (--max-removals / --allow-mass-removal).

Breaker rule: trips when planned removals exceed --max-removals (default 3),
OR when they would remove a majority (>50%) of current non-owner collaborators
(count >= 2). A single removal never trips it by itself.
"""

import json
from unittest.mock import MagicMock, PropertyMock, patch

from conftest import _make_args, _make_repo_json, _run_mocks

from addteam.app import _handle_apply, run
from addteam.models import Collaborator, TeamConfig


def _delete_calls(mock_run) -> list:
    return [c for c in mock_run.call_args_list if "DELETE" in c[0][0]]


def test_small_removal_unaffected(capsys):
    """2 removals from 5 collaborators (40%, under cap): no breaker involvement."""
    with (
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch(
            "addteam.app._get_collaborators_with_permissions",
            return_value={"alice": "push", "bob": "push", "carol": "push", "eve": "pull", "mallory": "pull"},
        ),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        config = TeamConfig(
            collaborators=[Collaborator("alice", "push"), Collaborator("bob", "push"), Collaborator("carol", "push")]
        )
        result = _handle_apply(_make_args(sync=True, json=True), config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    assert len(_delete_calls(mock_run)) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["removed"] == 2
    assert payload["circuit_breaker"]["would_trip"] is False
    assert payload["circuit_breaker"]["reason"] is None


def test_single_removal_never_trips(capsys):
    """1 removal from 2 collaborators (50%) is routine — never blocked."""
    with (
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value={"alice": "push", "eve": "pull"}),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(sync=True, json=True), config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    assert len(_delete_calls(mock_run)) == 1
    captured = capsys.readouterr()
    assert "refusing to remove" not in captured.err
    payload = json.loads(captured.out)
    assert payload["circuit_breaker"]["would_trip"] is False


def test_over_max_removals_blocks_noninteractive(capsys):
    """4 removals with max_removals=3 under --yes: blocked, exit 1, precise message."""
    collabs = {"alice": "push", "e1": "push", "e2": "push", "e3": "push", "e4": "push"}
    with (
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(
            _make_args(sync=True, yes=True, json=True), config, "owner", "repo", "owner/repo", "", "me"
        )

    captured = capsys.readouterr()
    assert result == 1
    assert _delete_calls(mock_run) == []
    assert "refusing to remove 4 of 5 collaborator(s)" in captured.err
    payload = json.loads(captured.out)
    assert payload["summary"]["removal_blocked"] == 4
    assert payload["removals"] == [
        {"username": u, "status": "blocked (circuit breaker)"} for u in ("e1", "e2", "e3", "e4")
    ]
    assert payload["circuit_breaker"]["would_trip"] is True
    assert payload["circuit_breaker"]["reason"] == "count"


def test_majority_removal_blocks_even_under_max(capsys):
    """4 removals from 6 collaborators (67%) with max_removals=5: count arm passes, majority arm trips."""
    collabs = {"alice": "push", "bob": "push", "e1": "push", "e2": "push", "e3": "push", "e4": "push"}
    with (
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push"), Collaborator("bob", "push")])
        args = _make_args(sync=True, yes=True, json=True, max_removals=5)
        result = _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")

    captured = capsys.readouterr()
    assert result == 1
    assert _delete_calls(mock_run) == []
    assert "refusing to remove" in captured.err
    payload = json.loads(captured.out)
    assert payload["summary"]["removal_blocked"] == 4
    assert payload["circuit_breaker"]["would_trip"] is True
    assert payload["circuit_breaker"]["reason"] == "majority"


def test_allow_mass_removal_bypasses_breaker():
    """--allow-mass-removal skips the breaker entirely (still subject to --yes/interactivity)."""
    collabs = {"alice": "push", "e1": "push", "e2": "push", "e3": "push", "e4": "push"}
    with (
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)) as mock_run,
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        args = _make_args(sync=True, yes=True, allow_mass_removal=True)
        result = _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    assert len(_delete_calls(mock_run)) == 4


def test_interactive_mass_removal_prompt_declined(capsys):
    """Interactive terminal + breaker trip + user says no: plain-decline semantics, exit 0."""
    collabs = {"alice": "push", "e1": "push", "e2": "push", "e3": "push", "e4": "push"}
    with (
        patch("addteam.app.confirm_mass_removal", return_value=False) as mock_mass_confirm,
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run") as mock_run,
        patch("sys.stdin.isatty", return_value=True),
        patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True),
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(sync=True, quiet=False), config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    mock_mass_confirm.assert_called_once()
    assert _delete_calls(mock_run) == []


def test_interactive_mass_removal_prompt_accepted():
    """Interactive + breaker trip + user says yes: all removals proceed; plain confirm not re-asked."""
    collabs = {"alice": "push", "e1": "push", "e2": "push", "e3": "push", "e4": "push"}
    with (
        patch("addteam.app.confirm_mass_removal", return_value=True) as mock_mass_confirm,
        patch("addteam.app.confirm_removals") as mock_plain_confirm,
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)) as mock_run,
        patch("sys.stdin.isatty", return_value=True),
        patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True),
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(sync=True, quiet=False), config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    mock_mass_confirm.assert_called_once()
    mock_plain_confirm.assert_not_called()
    assert len(_delete_calls(mock_run)) == 4


def test_dry_run_reports_would_trip(tmp_path, capsys):
    """--dry-run never blocks; it reports the would-trip state in text and in the payload."""
    collabs = {"alice": "push", "e1": "push", "e2": "push", "e3": "push", "e4": "push"}
    out_file = tmp_path / "plan.json"
    with (
        patch("addteam.app._get_pending_invitations", return_value=set()),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run") as mock_run,
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        args = _make_args(sync=True, dry_run=True, quiet=False, json_out=str(out_file))
        result = _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    mock_run.assert_not_called()
    assert "circuit breaker would trip" in capsys.readouterr().out
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["circuit_breaker"]["would_trip"] is True
    assert payload["circuit_breaker"]["reason"] == "count"
    assert payload["summary"]["removal_blocked"] == 0


def test_negative_max_removals_rejected(capsys):
    """--max-removals -1 is a usage error (exit 2) surfaced before any gh call."""
    result = run(["--max-removals", "-1"])

    assert result == 2
    assert "--max-removals cannot be negative" in capsys.readouterr().err


def test_max_removals_without_sync_is_noop(tmp_path, monkeypatch):
    """--max-removals without --sync is a silent no-op (same precedent as --fail-on-drift)."""
    mocks = _run_mocks() + [patch("addteam.app._run"), patch("addteam.app._create_welcome_issue")]
    with (
        mocks[0],
        mocks[1] as mock_json,
        mocks[2] as mock_text,
        mocks[3],
        mocks[4],
        mocks[5] as mock_run,
        mocks[6],
    ):
        mock_json.return_value = _make_repo_json()
        mock_text.return_value = "me"
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        (tmp_path / "team.yaml").write_text("developers:\n  - alice\n")
        monkeypatch.chdir(tmp_path)

        result = run(["--max-removals", "0"])

    assert result == 0
