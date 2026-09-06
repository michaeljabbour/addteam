"""v1.6.0: addteam accept (invitee-side)."""

import json
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from conftest import _today  # noqa: F401  (imported for parity with other test files; not required here)

from addteam.app import run


def _invitation(
    invitation_id: int = 1,
    full_name: str = "org/repo",
    inviter: str = "boss",
    permissions: str = "write",
    created_at: str = "2026-09-01T00:00:00Z",
    expired: bool = False,
) -> dict:
    owner, name = full_name.split("/", 1)
    return {
        "id": invitation_id,
        "repository": {"full_name": full_name, "name": name, "owner": {"login": owner}},
        "inviter": {"login": inviter},
        "permissions": permissions,
        "created_at": created_at,
        "expired": expired,
    }


@patch("addteam.app.shutil.which", return_value=None)
def test_dispatch_does_not_break_normal_run(_which, capsys):
    result = run(["owner/repo"])

    assert result == 1
    assert "GitHub CLI (gh) not found" in capsys.readouterr().err


def test_accept_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        run(["accept", "--help"])

    assert exc.value.code == 0


@patch("addteam.app.shutil.which", return_value=None)
def test_gh_not_found(_which, capsys):
    result = run(["accept"])

    assert result == 1
    assert "GitHub CLI (gh) not found" in capsys.readouterr().err


@patch("addteam.app._gh_api_paginated", return_value=[])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_no_pending_invitations(_which, _list, capsys):
    result = run(["accept", "-y"])

    assert result == 0
    assert "no pending invitations" in capsys.readouterr().out


@patch("addteam.app._run", return_value=MagicMock(returncode=0))
@patch("addteam.app._gh_api_paginated", return_value=[_invitation(invitation_id=123)])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_lists_and_accepts(_which, _list, mock_run, capsys):
    result = run(["accept", "-y"])

    assert result == 0
    mock_run.assert_called_once_with(["gh", "api", "-X", "PATCH", "user/repository_invitations/123"])
    assert "accepted" in capsys.readouterr().out


@patch("addteam.app._run", return_value=MagicMock(returncode=0))
@patch(
    "addteam.app._gh_api_paginated",
    return_value=[_invitation(invitation_id=1), _invitation(invitation_id=2, full_name="org/old", expired=True)],
)
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_expired_listed_not_accepted(_which, _list, mock_run, capsys):
    result = run(["accept", "-y"])

    assert result == 0
    assert mock_run.call_count == 1
    mock_run.assert_called_once_with(["gh", "api", "-X", "PATCH", "user/repository_invitations/1"])
    out = capsys.readouterr().out
    assert "ask the inviter to re-run addteam" in out
    assert "expired" in out


@patch("addteam.app._run")
@patch("addteam.app._gh_api_paginated", return_value=[_invitation(invitation_id=2, expired=True)])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_all_expired_no_accept_attempted(_which, _list, mock_run, capsys):
    result = run(["accept", "-y"])

    assert result == 0
    mock_run.assert_not_called()
    assert "ask the inviter to re-run addteam" in capsys.readouterr().out


@patch("addteam.app._run", return_value=MagicMock(returncode=0))
@patch(
    "addteam.app._gh_api_paginated",
    return_value=[
        _invitation(invitation_id=1, full_name="orgA/repo1"),
        _invitation(invitation_id=2, full_name="orgB/repo2"),
    ],
)
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_from_filter(_which, _list, mock_run, capsys):
    result = run(["accept", "--from", "ORGA", "-y"])

    assert result == 0
    mock_run.assert_called_once_with(["gh", "api", "-X", "PATCH", "user/repository_invitations/1"])
    out = capsys.readouterr().out
    assert "orgA/repo1" in out
    assert "orgB/repo2" not in out


@patch("addteam.app._run")
@patch("addteam.app._gh_api_paginated", return_value=[_invitation()])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_dry_run_no_mutation(_which, _list, mock_run, capsys):
    result = run(["accept", "-n"])

    assert result == 0
    mock_run.assert_not_called()
    assert "would accept 1 invitation(s)" in capsys.readouterr().out


@patch("addteam.app._run", return_value=MagicMock(returncode=0))
@patch("addteam.app._gh_api_paginated", return_value=[_invitation(invitation_id=7)])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_json_output_shape(_which, _list, mock_run, capsys):
    result = run(["accept", "-y", "--json"])

    assert result == 0
    mock_run.assert_called_once_with(["gh", "api", "-X", "PATCH", "user/repository_invitations/7"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "accept"
    assert payload["accepted"] == 1
    assert payload["failed"] == 0
    assert payload["results"] == [{"repo": "org/repo", "status": "accepted"}]
    assert payload["skipped_expired"] == 0


@patch("addteam.app.confirm_accept", return_value=False)
@patch("addteam.app._run")
@patch("addteam.app._gh_api_paginated", return_value=[_invitation()])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_decline_returns_zero(_which, _list, mock_run, mock_confirm, capsys):
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("rich.console.Console.is_terminal", new_callable=PropertyMock, return_value=True),
    ):
        result = run(["accept"])

    assert result == 0
    mock_confirm.assert_called_once_with(1)
    mock_run.assert_not_called()
    assert "not accepted" in capsys.readouterr().out


@patch("addteam.app._run", return_value=MagicMock(returncode=1))
@patch("addteam.app._gh_api_paginated", return_value=[_invitation(invitation_id=9)])
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_failed_patch_returns_1(_which, _list, mock_run, capsys):
    result = run(["accept", "-y"])

    assert result == 1
    mock_run.assert_called_once_with(["gh", "api", "-X", "PATCH", "user/repository_invitations/9"])
    assert "failed" in capsys.readouterr().out


@patch("addteam.app._gh_api_paginated", side_effect=RuntimeError("HTTP 401"))
@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_listing_failure_returns_1(_which, _list, capsys):
    result = run(["accept", "-y"])

    assert result == 1
    assert "HTTP 401" in capsys.readouterr().err
