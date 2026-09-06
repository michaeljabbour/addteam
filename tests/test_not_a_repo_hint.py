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
