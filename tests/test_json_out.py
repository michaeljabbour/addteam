"""v1.6.0: --json-out writes the run payload to a file, independent of --json."""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

from conftest import _audit_args, _make_args

from addteam.app import _handle_apply, _handle_audit, _run_metadata, run
from addteam.models import Collaborator, TeamConfig

_RUN_KEYS = {"version", "timestamp", "actor", "repo", "mode", "dry_run", "sync"}


def _apply_mocks(collabs):
    return (
        patch("addteam.app._get_pending_invitations", return_value={}),
        patch("addteam.app._get_collaborators_with_permissions", return_value=collabs),
        patch("addteam.app._run", return_value=MagicMock(returncode=0)),
    )


def test_json_out_writes_file_apply(tmp_path):
    """--json-out alone writes a valid payload with a `run` metadata block."""
    mock_pending, mock_collabs, mock_run = _apply_mocks({})
    out_file = tmp_path / "out.json"
    with mock_pending, mock_collabs, mock_run:
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_apply(_make_args(json_out=str(out_file)), config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["repo"] == "owner/repo"
    assert _RUN_KEYS <= set(payload["run"])
    assert payload["run"]["actor"] == "me"
    assert payload["run"]["mode"] == "apply"
    assert payload["run"]["dry_run"] is False
    assert payload["run"]["sync"] is False


def test_json_out_also_prints_human_output(tmp_path, capsys):
    """--json-out alone falls through to normal human-readable rendering (no early return)."""
    mock_pending, mock_collabs, mock_run = _apply_mocks({})
    out_file = tmp_path / "out.json"
    with mock_pending, mock_collabs, mock_run:
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        args = _make_args(json_out=str(out_file), quiet=False)
        result = _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    assert out_file.exists()
    out = capsys.readouterr().out
    assert "alice" in out
    assert "invited" in out


def test_json_out_plus_json_flag_does_both(tmp_path, capsys):
    """Both flags: file written AND stdout printed; --json keeps its early-return contract."""
    mock_pending, mock_collabs, mock_run = _apply_mocks({})
    out_file = tmp_path / "out.json"
    with mock_pending, mock_collabs, mock_run:
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        args = _make_args(json=True, json_out=str(out_file))
        result = _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")

    assert result == 0
    file_payload = json.loads(out_file.read_text(encoding="utf-8"))
    stdout_payload = json.loads(capsys.readouterr().out)
    assert file_payload["mode"] == "apply"
    assert stdout_payload["mode"] == "apply"
    assert file_payload == stdout_payload


def test_json_out_write_failure_returns_1(tmp_path, capsys):
    """A bad path (missing parent dir) errors cleanly with exit 1, like --csv."""
    mock_pending, mock_collabs, mock_run = _apply_mocks({})
    with mock_pending, mock_collabs, mock_run:
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        args = _make_args(json=True, json_out=str(tmp_path / "missing-dir" / "out.json"))
        result = _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")

    assert result == 1
    assert "could not write" in capsys.readouterr().err


def test_json_out_audit_mode(tmp_path):
    """--json-out also applies to --audit, with a `run` block."""
    out_file = tmp_path / "audit.json"
    with (
        patch("addteam.app._get_pending_invitations", return_value={}),
        patch("addteam.app._get_collaborators_with_permissions", return_value={}),
    ):
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])
        result = _handle_audit(config, "owner", "repo", "me", _audit_args(json_out=str(out_file)))

    assert result == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["mode"] == "audit"
    assert _RUN_KEYS <= set(payload["run"])
    assert payload["run"]["mode"] == "audit"


def test_json_out_rejected_with_report(tmp_path, capsys):
    """--report has no run payload; combining it with --json-out is a usage error."""
    result = run(["--report", str(tmp_path), "--json-out", str(tmp_path / "x.json")])

    assert result == 2
    assert "--json-out" in capsys.readouterr().err


def test_run_metadata_shape():
    """_run_metadata produces the documented 7-key run block with an ISO-Z timestamp."""
    meta = _run_metadata(repo_full_name="o/r", me="alice", mode="apply", dry_run=False, sync=True)

    assert set(meta) == _RUN_KEYS
    assert isinstance(meta["version"], str)
    assert isinstance(meta["timestamp"], str)
    assert meta["actor"] == "alice"
    assert meta["repo"] == "o/r"
    assert meta["mode"] == "apply"
    assert meta["dry_run"] is False
    assert meta["sync"] is True
    assert meta["timestamp"].endswith("Z")
    datetime.fromisoformat(meta["timestamp"].replace("Z", "+00:00"))
