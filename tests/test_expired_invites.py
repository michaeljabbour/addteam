"""v1.6.0: GitHub-side invitation expiry (distinct from config expires:)."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from conftest import _audit_args, _make_args, _make_paginated_router

from addteam.app import _handle_apply, _handle_audit
from addteam.gh import _invitation_age_days
from addteam.models import AuditResult, Collaborator, TeamConfig
from addteam.report import RepoAccess, ReportResult, _repo_access, matrix_lines, write_matrix_csv


def _pending(login, *, permission="push", expired=False, days_old=2, invite_id=7):
    """Rich pending-invitation dict in the shape _get_pending_invitations returns."""
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {login: {"id": invite_id, "permission": permission, "expired": expired, "created_at": created}}


def _apply(config, args):
    """Direct _handle_apply call against owner/repo as user 'me'."""
    return _handle_apply(args, config, "owner", "repo", "owner/repo", "", "me")


class TestInvitationAgeDays:
    def test_invitation_age_days_computes_delta(self):
        created = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        assert _invitation_age_days(created) == 5

    def test_invitation_age_days_handles_z_suffix(self):
        created = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
        assert _invitation_age_days(created) == 5


class TestApplyExpiredInvite:
    @patch("addteam.app._run")
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._get_pending_invitations")
    def test_expired_same_permission_reinvites(self, mock_pending, mock_collabs, mock_run, capsys):
        mock_pending.return_value = _pending("alice", expired=True)
        mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _apply(config, _make_args(json=True))

        assert result == 0
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert len(calls) == 2
        assert "DELETE" in calls[0] and any("invitations/7" in a for a in calls[0])
        assert "PUT" in calls[1] and any("permission=push" in a for a in calls[1])
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["status"] == "ok"
        assert payload["results"][0]["detail"] == "re-invited (expired)"

    @patch("addteam.app._run")
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._get_pending_invitations")
    def test_expired_dry_run_previews_reinvite(self, mock_pending, mock_collabs, mock_run, capsys):
        mock_pending.return_value = _pending("alice", expired=True)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _apply(config, _make_args(dry_run=True, json=True))

        assert result == 0
        mock_run.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["status"] == "would"
        assert payload["results"][0]["detail"] == "re-invite (expired)"

    @patch("addteam.app._run")
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._get_pending_invitations")
    def test_expired_and_mismatched_prefers_mismatch_message(self, mock_pending, mock_collabs, mock_run, capsys):
        mock_pending.return_value = _pending("alice", permission="admin", expired=True)
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _apply(config, _make_args(dry_run=True, json=True))

        assert result == 0
        mock_run.assert_not_called()
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["detail"] == "re-invite · stuck at admin, want push"

    @patch("addteam.app._run")
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._get_pending_invitations")
    def test_expired_delete_failure_reports_existing_message(self, mock_pending, mock_collabs, mock_run, capsys):
        mock_pending.return_value = _pending("alice", expired=True)
        mock_run.return_value = MagicMock(returncode=1, stderr="boom", stdout="")
        config = TeamConfig(collaborators=[Collaborator("alice", "push")])

        result = _apply(config, _make_args(json=True))

        assert result == 1
        assert mock_run.call_count == 1  # DELETE attempted, no PUT
        payload = json.loads(capsys.readouterr().out)
        assert payload["results"][0]["status"] == "fail"
        assert payload["results"][0]["detail"] == "could not replace stale invitation"

    @patch("addteam.app._create_welcome_issue", return_value=None)
    @patch("addteam.app._run")
    @patch("addteam.app._get_readme_excerpt", return_value=None)
    @patch("addteam.app._generate_repo_summary", return_value="summary text")
    @patch("addteam.app._get_collaborators_with_permissions", return_value={})
    @patch("addteam.app._get_pending_invitations")
    def test_prospective_includes_expired_same_permission(
        self, mock_pending, mock_collabs, mock_summary, mock_readme, mock_run, mock_welcome, monkeypatch
    ):
        mock_pending.return_value = _pending("alice", expired=True)
        mock_run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=0)]
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        config = TeamConfig(collaborators=[Collaborator("alice", "push")], welcome_issue=True)

        result = _apply(config, _make_args(no_ai=False))

        assert result == 0
        mock_summary.assert_called()


class TestAuditExpiredInvite:
    @patch("addteam.app._get_pending_invitations")
    @patch("addteam.app._audit_collaborators")
    def test_audit_shows_pending_with_age(self, mock_audit, mock_pending, capsys):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        mock_pending.return_value = _pending("bob", expired=False, days_old=3)

        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args())

        assert result == 0
        assert "pending 3d" in capsys.readouterr().out

    @patch("addteam.app._get_pending_invitations")
    @patch("addteam.app._audit_collaborators")
    def test_audit_shows_expired_distinctly(self, mock_audit, mock_pending, capsys):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        mock_pending.return_value = _pending("bob", expired=True)

        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args())

        assert result == 0
        out = capsys.readouterr().out
        assert "(expired)" in out
        assert "pending" not in out

    @patch("addteam.app._get_pending_invitations")
    @patch("addteam.app._audit_collaborators")
    def test_audit_json_includes_invite_expired(self, mock_audit, mock_pending, capsys):
        mock_audit.return_value = AuditResult(missing=[Collaborator("bob", "push")])
        mock_pending.return_value = _pending("bob", expired=True)

        result = _handle_audit(TeamConfig(), "owner", "repo", "me", _audit_args(json=True))

        assert result == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["missing"][0]["invite_pending"] is True
        assert payload["missing"][0]["invite_expired"] is True


class TestReportExpiredInvite:
    @patch("addteam.report._gh_api_paginated")
    def test_report_repo_access_tags_expired_status(self, mock_pages):
        mock_pages.side_effect = _make_paginated_router(
            {
                "collaborators": [],
                "invitations": [
                    {
                        "id": 7,
                        "invitee": {"login": "carol"},
                        "permissions": "write",
                        "expired": True,
                        "created_at": "2024-01-15T10:00:00Z",
                    }
                ],
            }
        )

        rows = _repo_access("o/r")

        assert len(rows) == 1
        assert rows[0].username == "carol"
        assert rows[0].status == "expired"
        assert rows[0].invited_at == "2024-01-15"
        assert rows[0].permission == "push"

    def test_matrix_lines_uses_star_and_bang(self):
        result = ReportResult(
            rows=[
                RepoAccess(repo="o/r", username="bob", permission="push", status="pending"),
                RepoAccess(repo="o/r", username="alice", permission="push", status="expired"),
            ]
        )

        header, lines = matrix_lines(result)

        assert header == ["user", "r"]
        assert lines == [["alice", "!"], ["bob", "*"]]

    def test_write_matrix_csv_expired_label(self, tmp_path):
        result = ReportResult(rows=[RepoAccess(repo="o/r", username="alice", permission="push", status="expired")])
        out = tmp_path / "matrix.csv"

        write_matrix_csv(result, out)

        assert "push (expired)" in out.read_text()
