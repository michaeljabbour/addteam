"""Shared test fixtures and mock-builder helpers for addteam's test suite.

Centralizes the mock-gh patterns used across test_bootstrap.py and the
per-feature test files added in v1.6.0, so no test file re-implements them.
These are plain helper functions (not pytest fixtures) — pytest's default
"prepend" import mode puts this file's directory on sys.path, so sibling
test files import them with `from conftest import ...`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date
from unittest.mock import patch


def _today() -> date:
    """Naive local today — matches what the app compares expiry dates against."""
    return date.today()  # noqa: DTZ011  expiry dates are naive by design


def _make_args(**overrides) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for direct _handle_apply(...) calls.

    Every key here is a real CLI flag `_handle_apply` (defensively, via
    getattr) or the argparse parser itself may read. Add a new key here
    whenever a feature adds a new flag `_handle_apply` consumes — do not
    add ad-hoc Namespace construction in a new test file.
    """
    defaults = {
        "dry_run": False,
        "sync": False,
        "quiet": True,
        "no_ai": True,
        "no_welcome": True,
        "provider": "auto",
        "json": False,
        "yes": False,
        "json_out": None,
        "max_removals": 3,
        "allow_mass_removal": False,
        "sync_teams": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _audit_args(**overrides) -> argparse.Namespace:
    """Build a minimal argparse.Namespace for direct _handle_audit(...) calls."""
    defaults = {"json": False, "fail_on_drift": False, "quiet": False, "json_out": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run_mocks() -> list:
    """The 5 patches every full run()-level CLI test needs, in fixed order:
    shutil.which, _gh_json, _gh_text, _get_pending_invitations, _get_collaborators_with_permissions.

    Usage (matches the existing test_bootstrap.py convention exactly):
        mocks = _run_mocks()
        with mocks[0], mocks[1] as mock_json, mocks[2] as mock_text, mocks[3], mocks[4]:
            mock_json.return_value = _make_repo_json()
            mock_text.return_value = "me"
            ...
    Extend for a specific test with `_run_mocks() + [patch("addteam.app._run")]`
    (already the convention used by TestPersonalRepoPreflight etc.).
    """
    return [
        patch("addteam.app.shutil.which", return_value="/usr/bin/gh"),
        patch("addteam.app._gh_json"),
        patch("addteam.app._gh_text"),
        patch("addteam.app._get_pending_invitations", return_value={}),
        patch("addteam.app._get_collaborators_with_permissions", return_value={}),
    ]


def _make_repo_json(*, in_org: bool = True, name: str = "repo", owner: str = "owner", description: str = "") -> dict:
    """Payload shape for `gh repo view --json name,owner,description,isInOrganization`."""
    return {
        "name": name,
        "owner": {"login": owner},
        "description": description,
        "isInOrganization": in_org,
    }


def _make_paginated_router(mapping: dict[str, list[dict]]) -> Callable:
    """Build a `_gh_api_paginated` side_effect that routes on a substring
    matched against ' '.join(args) — so query-string filters like
    '-f role=maintainer' can be distinguished, not just the endpoint path.

    mapping: {substring: items_to_return}, first match (in dict iteration
    order) wins. Raises RuntimeError if nothing matches, so an unmocked
    endpoint fails loudly instead of silently returning [].
    """

    def _route(args, *, what):
        joined = " ".join(args)
        for key, items in mapping.items():
            if key in joined:
                return items
        raise RuntimeError(f"_make_paginated_router: no route matched {joined!r} (what={what!r})")

    return _route
