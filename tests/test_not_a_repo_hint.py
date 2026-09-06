"""Not-a-git-repo guidance: the hint block reflects the real report sources.

When the cwd contains cloned repos, the hint should lead with `addteam --report .`;
otherwise it should only offer the always-applicable sources (--report DIR, --org, --repos).
"""

import pytest

from addteam.app import run


@pytest.fixture
def not_a_repo_mocks():
    from unittest.mock import patch

    with (
        patch("addteam.app.shutil.which") as mock_which,
        patch("addteam.app._gh_json") as mock_json,
    ):
        mock_which.return_value = "/usr/bin/gh"
        mock_json.side_effect = RuntimeError("Failed to resolve repo: failed to run git: fatal: not a git repository")
        yield


def test_hint_offers_report_dot_when_cwd_holds_repos(tmp_path, monkeypatch, capsys, not_a_repo_mocks):
    (tmp_path / "alpha" / ".git").mkdir(parents=True)
    (tmp_path / "beta" / ".git").mkdir(parents=True)
    (tmp_path / "not-a-repo").mkdir()
    monkeypatch.chdir(tmp_path)

    result = run(["--audit"])

    err = capsys.readouterr().err
    assert result == 1
    assert "This folder contains 2 git repos" in err
    assert "--report ." in err
    assert "--org NAME" in err


def test_hint_omits_report_dot_when_no_repos(tmp_path, monkeypatch, capsys, not_a_repo_mocks):
    monkeypatch.chdir(tmp_path)

    result = run(["--audit"])

    err = capsys.readouterr().err
    assert result == 1
    assert "This folder contains" not in err
    assert "--org NAME" in err
    assert "--repos repos.txt" in err


def test_no_config_hint_offers_snapshot_from_current(tmp_path, monkeypatch, capsys):
    """In a repo with no team.yaml, the hint must mention --init --from-current."""
    from unittest.mock import patch

    from conftest import _make_repo_json

    (tmp_path / ".git").mkdir()  # git-ish dir, no team.yaml
    monkeypatch.chdir(tmp_path)

    with (
        patch("addteam.app.shutil.which", return_value="/usr/bin/gh"),
        patch("addteam.app._gh_json") as mock_json,
        patch("addteam.config._gh_read_repo_file") as mock_read,
    ):
        mock_json.return_value = _make_repo_json()
        mock_read.side_effect = RuntimeError("failed to run gh api: HTTP 404: Not Found")

        result = run(["--audit"])

    err = capsys.readouterr().err
    assert result == 1
    assert "No team config found." in err
    assert "addteam --init" in err
    assert "addteam --init --from-current" in err
