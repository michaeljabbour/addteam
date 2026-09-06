"""v1.6.0: --org/--repos report sources, visibility/fork/archived, matrix initials."""

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import _make_paginated_router

from addteam.app import run
from addteam.report import (
    RepoAccess,
    ReportResult,
    _build_report_from_slugs,
    _list_org_repos,
    _parse_repo_list_txt,
    matrix_lines,
    write_long_csv,
)

_ORG_LIST_JSON = json.dumps(
    [
        {"nameWithOwner": "myorg/api", "isFork": True, "isArchived": False, "visibility": "PUBLIC"},
        {"nameWithOwner": "myorg/web", "isFork": False, "isArchived": False, "visibility": "PRIVATE"},
    ]
)

_ORG_LIST_WITH_ARCHIVED = json.dumps(
    [
        {"nameWithOwner": "myorg/old", "isFork": False, "isArchived": True, "visibility": "PUBLIC"},
    ]
)


def _gh_run_with_org_list(org_list_json):
    """Fake addteam.report._run: `gh repo list` returns the fixed JSON; user lookups succeed empty."""

    def _call(args, cwd=None):
        if args[:2] == ["gh", "api"]:
            return MagicMock(returncode=0, stdout="\n", stderr="")
        return MagicMock(returncode=0, stdout=org_list_json, stderr="")

    return _call


def test_parse_repo_list_txt_skips_comments_and_blanks():
    text = "# header comment\n\nowner/a\n   \nowner/b\n# trailing\n"
    assert _parse_repo_list_txt(text) == ["owner/a", "owner/b"]


def test_parse_repo_list_txt_dedupes():
    text = "owner/a\nowner/b\nowner/a\n"
    assert _parse_repo_list_txt(text) == ["owner/a", "owner/b"]


@patch("addteam.report._run")
def test_list_org_repos_excludes_forks_by_default(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_ORG_LIST_JSON, stderr="")
    slugs, _meta = _list_org_repos("myorg", include_forks=False)
    assert slugs == ["myorg/web"]


@patch("addteam.report._run")
def test_list_org_repos_include_forks(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_ORG_LIST_JSON, stderr="")
    slugs, _meta = _list_org_repos("myorg", include_forks=True)
    assert slugs == ["myorg/api", "myorg/web"]


@patch("addteam.report._run")
def test_list_org_repos_meta_shape(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout=_ORG_LIST_JSON, stderr="")
    slugs, meta = _list_org_repos("myorg", include_forks=True)
    assert "myorg/web" in slugs
    assert meta["myorg/web"] == {"visibility": "private", "fork": False, "archived": False}


@patch("addteam.report._run")
def test_list_org_repos_failure_raises(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="HTTP 403")
    with pytest.raises(RuntimeError, match="could not list repos for org myorg"):
        _list_org_repos("myorg", include_forks=False)


@patch("addteam.report._repo_access")
def test_build_report_from_slugs_applies_meta(mock_access):
    mock_access.return_value = [RepoAccess(repo="o/r", username="alice", permission="push")]
    result = _build_report_from_slugs(
        ["o/r"],
        include_names=False,
        repo_meta={"o/r": {"visibility": "public", "fork": True, "archived": False}},
    )
    row = result.rows[0]
    assert row.visibility == "public"
    assert row.fork is True


@patch("addteam.report._repo_access")
def test_build_report_from_slugs_tolerates_repo_access_failure(mock_access):
    mock_access.side_effect = RuntimeError("HTTP 403")
    result = _build_report_from_slugs(["o/a", "o/b"], include_names=False)
    assert result.repo_failures == ["o/a", "o/b"]
    assert result.repos_seen == 2


def test_org_and_report_mutually_exclusive(capsys):
    result = run(["--report", "/tmp", "--org", "myorg"])
    assert result == 2
    assert "mutually exclusive" in capsys.readouterr().err


@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
def test_repos_file_must_exist(_which, capsys):
    result = run(["--repos", "/definitely/not/a/file"])
    assert result == 2
    assert "not a file" in capsys.readouterr().err


@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
@patch("addteam.report._run")
@patch("addteam.report._gh_api_paginated")
def test_org_end_to_end_json(mock_api, mock_run, _which, capsys):
    mock_run.side_effect = _gh_run_with_org_list(_ORG_LIST_JSON)
    mock_api.side_effect = _make_paginated_router(
        {"collaborators": [{"login": "alice", "role_name": "push"}], "invitations": []}
    )
    result = run(["--org", "myorg", "--json"])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["visibility"] == "private"
    assert payload["rows"][0]["fork"] is False


@patch("addteam.app.shutil.which", return_value="/usr/bin/gh")
@patch("addteam.report._run")
@patch("addteam.report._gh_api_paginated")
def test_archived_repo_flagged(mock_api, mock_run, _which, capsys):
    mock_run.side_effect = _gh_run_with_org_list(_ORG_LIST_WITH_ARCHIVED)
    mock_api.side_effect = _make_paginated_router(
        {"collaborators": [{"login": "alice", "role_name": "push"}], "invitations": []}
    )
    result = run(["--org", "myorg", "--json"])
    assert result == 0
    captured = capsys.readouterr()
    assert "is archived" in captured.err
    payload = json.loads(captured.out)
    assert payload["summary"]["archived_repos"] == ["myorg/old"]


def test_matrix_lines_permission_initial():
    result = ReportResult(rows=[RepoAccess(repo="o/r", username="alice", permission="push")])
    header, lines = matrix_lines(result)
    assert header == ["user", "r"]
    assert lines == [["alice", "p"]]


def test_matrix_lines_still_marks_pending_and_expired():
    result = ReportResult(
        rows=[
            RepoAccess(repo="o/r", username="alice", permission="push"),
            RepoAccess(repo="o/r", username="bob", permission="pull", status="pending"),
            RepoAccess(repo="o/r", username="carol", permission="pull", status="expired"),
        ]
    )
    _header, lines = matrix_lines(result)
    by_user = {line[0]: line[1] for line in lines}
    assert by_user == {"alice": "p", "bob": "*", "carol": "!"}


def test_long_csv_includes_new_columns(tmp_path):
    out = tmp_path / "report.csv"
    write_long_csv(
        ReportResult(
            rows=[RepoAccess(repo="o/r", username="alice", permission="push", visibility="private", fork=True)]
        ),
        out,
    )
    lines = out.read_text().splitlines()
    assert lines[0] == "repo,username,name,permission,status,invited_at,visibility,fork,archived"
    assert "private,True,False" in lines[1]
