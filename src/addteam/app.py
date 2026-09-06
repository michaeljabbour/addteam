"""CLI application: argument parsing, audit/apply/init handlers."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.table import Table

from . import __version__
from .ai import _generate_repo_summary, available_providers
from .config import _is_valid_repo_spec, _resolve_team_config
from .console import console, err_console, error, warning
from .gh import (
    _create_welcome_issue,
    _get_collaborators_with_permissions,
    _get_pending_invitations,
    _get_readme_excerpt,
    _gh_json,
    _gh_text,
    _invitation_age_days,
    _run,
)
from .models import GITHUB_PERMISSION_MAP, ROLE_PERMISSIONS, VALID_PERMISSIONS, AuditResult, Collaborator, TeamConfig
from .report import build_report, matrix_lines, write_long_csv, write_matrix_csv
from .templates import GITHUB_ACTION_MULTI_REPO_TEMPLATE, GITHUB_ACTION_TEMPLATE, REPOS_TXT_TEMPLATE, TEAM_YAML_TEMPLATE
from .ui import check_for_updates, confirm_mass_removal, confirm_removals, print_config, print_header, print_separator

# =============================================================================
# Run metadata
# =============================================================================


def _run_metadata(*, repo_full_name: str, me: str, mode: str, dry_run: bool, sync: bool) -> dict:
    """The `run` metadata block embedded in --json/--json-out payloads (apply/audit/dry-run)."""
    return {
        "version": __version__,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "actor": me,
        "repo": repo_full_name,
        "mode": mode,
        "dry_run": dry_run,
        "sync": sync,
    }


# =============================================================================
# Init Commands
# =============================================================================


def _init_team_yaml(repo_name: str, owner: str) -> Path:
    """Create a starter team.yaml file."""
    path = Path("team.yaml")
    if path.exists():
        raise FileExistsError(f"{path} already exists")

    content = TEAM_YAML_TEMPLATE.format(repo_name=repo_name, owner=owner)
    path.write_text(content)
    return path


def _init_github_action(multi_repo: bool = False) -> Path:
    """Create GitHub Action workflow for syncing collaborators."""
    workflows_dir = Path(".github/workflows")
    workflows_dir.mkdir(parents=True, exist_ok=True)

    if multi_repo:
        path = workflows_dir / "sync-team.yml"
        path.write_text(GITHUB_ACTION_MULTI_REPO_TEMPLATE)

        repos_txt = Path("repos.txt")
        if not repos_txt.exists():
            repos_txt.write_text(REPOS_TXT_TEMPLATE)
    else:
        path = workflows_dir / "sync-collaborators.yml"
        path.write_text(GITHUB_ACTION_TEMPLATE)

    return path


def _handle_init(args: argparse.Namespace) -> int:
    """Handle --init, --init-action, and --init-multi-repo commands."""
    repo_name = "my-repo"
    owner = "your-username"

    try:
        repo = _gh_json(["repo", "view", "--json", "name,owner"], what="get repo info")
        if isinstance(repo, dict):
            repo_name = repo["name"]
            owner = repo["owner"]["login"]
    except RuntimeError:
        pass  # Not in a repo or gh not authenticated, use defaults

    created_files = []

    if args.init:
        try:
            path = _init_team_yaml(repo_name, owner)
            created_files.append(str(path))
        except FileExistsError as exc:
            console.print(f"[yellow]skip:[/yellow] {exc}")

    if args.init_action:
        path = _init_github_action(multi_repo=False)
        created_files.append(str(path))

    if args.init_multi_repo:
        path = _init_github_action(multi_repo=True)
        created_files.append(str(path))
        if Path("repos.txt").exists():
            console.print("[dim]repos.txt already exists[/dim]")
        else:
            created_files.append("repos.txt")

    if created_files:
        console.print()
        console.print("[green]✓[/green] Created:")
        for f in created_files:
            console.print(f"  {f}")
        console.print()
        console.print("[dim]Next steps:[/dim]")
        console.print("  1. Edit team.yaml with your team members")
        console.print("  2. Commit and push")
        if args.init_action or args.init_multi_repo:
            console.print("  3. Add TEAM_SYNC_TOKEN secret to GitHub repo")
            console.print("     (PAT with repo + admin:org scopes)")
        console.print()

    return 0


# =============================================================================
# Audit
# =============================================================================


def _audit_collaborators(config: TeamConfig, repo_owner: str, repo_name: str, me: str) -> AuditResult:
    """Compare desired state (config) with actual state (GitHub)."""
    result = AuditResult()
    current = _get_collaborators_with_permissions(repo_owner, repo_name)

    desired: dict[str, Collaborator] = {}
    for collab in config.collaborators:
        if collab.username == repo_owner or collab.username == me:
            continue
        if collab.is_expired:
            result.expired.append(collab)
        else:
            desired[collab.username.casefold()] = collab

    current_lower = {u.casefold(): (u, perm) for u, perm in current.items()}

    for username_lower, collab in desired.items():
        entry = current_lower.get(username_lower)
        if entry:
            current_user, current_perm = entry
            if current_perm != collab.permission:
                result.permission_drift.append((current_user, current_perm, collab.permission))
        else:
            result.missing.append(collab)

    for current_user in current:
        if current_user == repo_owner or current_user == me:
            continue
        if current_user.casefold() not in desired:
            result.extra.append(current_user)

    return result


def _handle_audit(
    config: TeamConfig,
    repo_owner: str,
    repo_name: str,
    me: str,
    args: argparse.Namespace | None = None,
) -> int:
    """Handle --audit mode: show drift without making changes.

    `args` is optional for direct callers; when provided (as run() does), audit
    also annotates users whose invitation is still pending (or expired) and
    honors --fail-on-drift / --json.
    """
    audit = _audit_collaborators(config, repo_owner, repo_name, me)

    json_mode = bool(getattr(args, "json", False))
    fail_on_drift = bool(getattr(args, "fail_on_drift", False))

    pending: dict[str, dict] = {}
    if args is not None and audit.missing:
        pending = _get_pending_invitations(repo_owner, repo_name)
    pending_lower = _normalize_pending(pending)

    exit_code = 1 if (fail_on_drift and audit.drift_count) else 0

    json_out = getattr(args, "json_out", None)
    if json_mode or json_out:
        payload = {
            "mode": "audit",
            "repo": f"{repo_owner}/{repo_name}",
            "source": config.source,
            "drift": audit.drift_count,
            "missing": [
                {
                    "username": c.username,
                    "name": c.name,
                    "permission": c.permission,
                    "from_team": c.from_team,
                    "invite_pending": c.username.casefold() in pending_lower,
                    "invite_expired": bool(pending_lower.get(c.username.casefold(), {}).get("expired")),
                }
                for c in audit.missing
            ],
            "extra": audit.extra,
            "permission_drift": [
                {"username": u, "current": has, "expected": should} for u, has, should in audit.permission_drift
            ],
            "expired": [{"username": c.username, "name": c.name, "expires": str(c.expires)} for c in audit.expired],
            "warnings": config.warnings,
            "run": _run_metadata(
                repo_full_name=f"{repo_owner}/{repo_name}", me=me, mode="audit", dry_run=False, sync=False
            ),
        }
        rendered = json.dumps(payload, indent=2)
        if json_out:
            try:
                Path(json_out).expanduser().write_text(rendered + "\n", encoding="utf-8")
            except OSError as exc:
                error(f"could not write {json_out}: {exc}")
                return 1
        if json_mode:
            print(rendered)
            return exit_code

    if audit.drift_count == 0:
        console.print("  [green]✓ no drift detected[/green]")
        console.print()
        return exit_code

    console.print("  [yellow]⚠ drift detected[/yellow]")
    console.print()

    if audit.missing:
        console.print("  [bold]Missing[/bold] (should have access):")
        for c in audit.missing:
            team_note = f" [dim]from {escape(c.from_team)}[/dim]" if c.from_team else ""
            info = pending_lower.get(c.username.casefold())
            pending_note = ""
            if info is not None:
                if info.get("expired"):
                    pending_note = " [dim](expired)[/dim]"
                elif info.get("created_at"):
                    age = _invitation_age_days(info["created_at"])
                    pending_note = f" [dim](pending {age}d)[/dim]"
                else:
                    pending_note = " [dim](pending)[/dim]"
            name_note = f" [dim italic]{escape(c.name)}[/dim italic]" if c.name else ""
            console.print(
                f"    [green]+[/green] {escape(c.username)} ({c.permission}){team_note}{pending_note}{name_note}"
            )
        console.print()

    if audit.extra:
        console.print("  [bold]Extra[/bold] (should not have access):")
        for u in audit.extra:
            console.print(f"    [red]-[/red] {escape(u)}")
        console.print()

    if audit.permission_drift:
        console.print("  [bold]Permission drift[/bold]:")
        for user, has, should in audit.permission_drift:
            console.print(f"    [yellow]~[/yellow] {escape(user)}: {has} → {should}")
        console.print()

    if audit.expired:
        console.print("  [bold]Expired[/bold] (should be removed):")
        for c in audit.expired:
            name_note = f" [dim italic]{escape(c.name)}[/dim italic]" if c.name else ""
            console.print(f"    [red]⏰[/red] {escape(c.username)} (expired {c.expires}){name_note}")
        console.print()

    print_separator()
    console.print(f"  [bold]total drift:[/bold] {audit.drift_count} item(s)")
    console.print()
    console.print("  [dim]run without --audit to apply changes[/dim]")
    console.print()
    return exit_code


# =============================================================================
# Apply
# =============================================================================


def _normalize_pending(pending: Any) -> dict[str, dict]:
    """Normalize _get_pending_invitations' return value to a casefold-keyed dict.

    Tolerates legacy plain-set/list values (e.g. in tests/other callers) by
    treating each entry as a bare login with no extra info. Used by both
    _handle_apply and _handle_audit so the tolerance logic lives in one place.
    """
    if isinstance(pending, dict):
        return {login.casefold(): info for login, info in pending.items()}
    return {login.casefold(): {} for login in pending}


def _handle_apply(
    args: argparse.Namespace,
    config: TeamConfig,
    repo_owner: str,
    repo_name: str,
    repo_full_name: str,
    description: str,
    me: str,
) -> int:
    """Handle default apply mode: invite/remove collaborators, fix permission drift."""
    added = 0
    updated = 0
    skipped = 0
    failed = 0
    removed = 0
    remove_failed = 0
    welcomed = 0
    blocked_count = 0
    removal_count = 0
    total_non_owner = 0
    removal_pct = 0.0
    breaker_would_trip = False
    breaker_reason: str | None = None
    max_removals = getattr(args, "max_removals", 3)
    allow_mass = bool(getattr(args, "allow_mass_removal", False))
    results: list[tuple[str, str, str]] = []
    removals: list[tuple[str, str]] = []

    json_mode = bool(getattr(args, "json", False))
    human = not args.quiet and not json_mode
    display_names = {c.username: c.name for c in config.collaborators if c.name}

    # Defense in depth for direct callers: run() also guards this.
    if args.sync and config.incomplete:
        error("config could not be fully resolved — refusing to --sync against partial state")
        return 1

    # Fetch existing collaborators (accepted) and pending invitations
    status = console.status("  Fetching collaborators...", spinner="dots") if human else None
    if status:
        status.start()
    try:
        try:
            existing_collabs = _get_collaborators_with_permissions(repo_owner, repo_name)
        except RuntimeError as exc:
            if status:
                status.stop()
            error(f"could not fetch collaborators: {exc}")
            return 1
        existing_lower = {u.casefold(): (u, perm) for u, perm in existing_collabs.items()}

        pending_invites: Any = _get_pending_invitations(repo_owner, repo_name)
        pending_map = _normalize_pending(pending_invites)
    finally:
        if status:
            status.stop()

    def _pending_perm(entry: Any) -> str | None:
        return entry.get("permission") if isinstance(entry, dict) else None

    def _pending_expired(entry: Any) -> bool:
        return bool(entry.get("expired")) if isinstance(entry, dict) else False

    # Who will actually get a (re-)invite? Welcome issues and the AI summary
    # exist for them only — on a fully-converged repo we skip the AI call
    # entirely (saves tokens and output noise).
    prospective: list[Collaborator] = []
    for collab in config.collaborators:
        cf = collab.username.casefold()
        if collab.username == repo_owner or collab.username == me or collab.is_expired:
            continue
        if cf in existing_lower:
            continue  # already has access (maybe an update — no welcome needed)
        pending_entry = pending_map.get(cf)
        if (
            pending_entry is not None
            and _pending_perm(pending_entry) == collab.permission
            and not _pending_expired(pending_entry)
        ):
            continue  # already invited at the right level, invite still valid
        prospective.append(collab)

    # Generate AI summary only when welcome issues will actually be sent
    ai_summary: str | None = None
    if config.welcome_issue and not args.no_ai and prospective:
        if not available_providers() and args.provider == "auto":
            if human:
                console.print("  [dim]ai[/dim]          no API keys found")
                console.print()
        else:
            readme_content = _get_readme_excerpt(repo_owner, repo_name, max_lines=100)
            providers_to_try = [args.provider] if args.provider != "auto" else available_providers()
            for provider in providers_to_try:
                try:
                    ai_summary = _generate_repo_summary(
                        provider=provider,
                        repo_full_name=repo_full_name,
                        repo_description=description,
                        readme_content=readme_content,
                    )
                    if human:
                        console.print(f"  [dim]ai[/dim]          {provider} ✓")
                        console.print()
                    break
                except RuntimeError as e:
                    if human:
                        console.print(f"  [dim]ai[/dim]          {provider} failed: {str(e)[:50]}")
                    continue

            if not ai_summary and human:
                console.print()  # blank line after failed attempts

    # Process collaborators
    for collab in config.collaborators:
        u = collab.username
        reinvite_reason: str | None = None  # disambiguates "invited" vs "re-invited (expired)" below

        if u == repo_owner:
            results.append((u, "skip", "owner"))
            skipped += 1
            continue
        if u == me:
            results.append((u, "skip", "you"))
            skipped += 1
            continue
        if collab.is_expired:
            results.append((u, "skip", f"expired {collab.expires}"))
            skipped += 1
            continue

        # Already has access: skip, or fix permission drift to converge state
        entry = existing_lower.get(u.casefold())
        if entry:
            actual_user, current_perm = entry
            if current_perm != collab.permission:
                if args.dry_run:
                    results.append((u, "would", f"update · {current_perm} → {collab.permission}"))
                    updated += 1
                    continue
                r = _run(
                    [
                        "gh",
                        "api",
                        "-X",
                        "PUT",
                        f"repos/{repo_owner}/{repo_name}/collaborators/{actual_user}",
                        "-f",
                        f"permission={collab.permission}",
                    ]
                )
                if r.returncode == 0:
                    # GitHub can 2xx-and-silently-ignore permission updates (e.g.
                    # maintain/triage on personal repos) — verify after write.
                    applied = current_perm
                    check = _run(
                        ["gh", "api", f"repos/{repo_owner}/{repo_name}/collaborators/{actual_user}/permission"]
                    )
                    if check.returncode == 0:
                        try:
                            applied_raw = json.loads(check.stdout).get("permission", "")
                        except (json.JSONDecodeError, TypeError, AttributeError):
                            applied_raw = ""
                        applied = GITHUB_PERMISSION_MAP.get(applied_raw, applied_raw) or current_perm
                    if applied != collab.permission:
                        results.append((u, "fail", f"update ignored by GitHub (still {applied})"))
                        failed += 1
                    else:
                        results.append((u, "updated", f"{current_perm} → {collab.permission}"))
                        updated += 1
                else:
                    details = r.stderr.strip() or r.stdout.strip() or "unknown"
                    results.append((u, "fail", f"update failed: {details}"))
                    failed += 1
                continue
            results.append((u, "skip", "already has access"))
            skipped += 1
            continue

        # Already invited (pending acceptance) — GitHub can't edit a pending
        # invitation, so a permission mismatch OR an expired invite needs
        # delete + re-invite.
        pending_entry = pending_map.get(u.casefold())
        if pending_entry is not None:
            pending_perm = pending_entry.get("permission") if isinstance(pending_entry, dict) else None
            pending_id = pending_entry.get("id") if isinstance(pending_entry, dict) else None
            pending_expired = _pending_expired(pending_entry)
            pending_mismatch = bool(pending_perm and pending_perm != collab.permission)
            if pending_mismatch or pending_expired:
                if args.dry_run:
                    if pending_mismatch:
                        results.append((u, "would", f"re-invite · stuck at {pending_perm}, want {collab.permission}"))
                        updated += 1
                    else:
                        results.append((u, "would", "re-invite (expired)"))
                        added += 1
                    continue
                r_del = _run(["gh", "api", "-X", "DELETE", f"repos/{repo_owner}/{repo_name}/invitations/{pending_id}"])
                if r_del.returncode != 0:
                    results.append((u, "fail", "could not replace stale invitation"))
                    failed += 1
                    continue
                # fall through to a fresh invite below; mismatch keeps the
                # normal "invited · perm" message below, expired-only (same
                # permission, just stale) gets its own message.
                reinvite_reason = None if pending_mismatch else "expired"
            else:
                results.append((u, "skip", "already invited"))
                skipped += 1
                continue

        if args.dry_run:
            team_note = f" · {collab.from_team}" if collab.from_team else ""
            results.append((u, "would", f"invite · {collab.permission}{team_note}"))
            added += 1
            continue

        r = _run(
            [
                "gh",
                "api",
                "-X",
                "PUT",
                f"repos/{repo_owner}/{repo_name}/collaborators/{u}",
                "-f",
                f"permission={collab.permission}",
            ]
        )

        if r.returncode == 0:
            if reinvite_reason == "expired":
                detail = "re-invited (expired)"
            else:
                team_note = f" · {collab.from_team}" if collab.from_team else ""
                detail = f"invited · {collab.permission}{team_note}"
            results.append((u, "ok", detail))
            added += 1

            if config.welcome_issue:
                issue_url = _create_welcome_issue(
                    repo_owner,
                    repo_name,
                    u,
                    config.welcome_message or ai_summary,
                    collab.permission,
                    collab.name,
                )
                if issue_url:
                    welcomed += 1
        else:
            details = r.stderr.strip() or r.stdout.strip() or "unknown"
            results.append((u, "fail", details))
            failed += 1

    # Print results
    if human:
        symbols = {
            "ok": "[green]✓[/green]",
            "updated": "[yellow]~[/yellow]",
            "would": "[blue]○[/blue]",
            "skip": "[dim]·[/dim]",
            "fail": "[red]✗[/red]",
        }
        for user, status_name, detail in results:
            symbol = symbols.get(status_name, "?")
            line = f"  {symbol} {escape(user):<22} "
            if status_name == "fail":
                line += f"[red]{escape(detail)}[/red]"
            else:
                line += f"[dim]{escape(detail)}[/dim]"
            if name := display_names.get(user):
                line += f"  [dim italic]{escape(name)}[/dim italic]"
            console.print(line)
        console.print()

    # Sync mode: remove extras and expired
    removals_declined = False
    breaker_blocked = False
    if args.sync:
        try:
            current_collabs = set(_get_collaborators_with_permissions(repo_owner, repo_name).keys())
        except RuntimeError as exc:
            error(str(exc))
            return 1

        current_collabs.discard(repo_owner)
        current_collabs.discard(me)
        total_non_owner = len(current_collabs)

        valid_users = {c.username.casefold() for c in config.collaborators if not c.is_expired}
        to_remove = sorted(u for u in current_collabs if u.casefold() not in valid_users)

        expired_users = [c.username for c in config.collaborators if c.is_expired]
        for eu in expired_users:
            if eu.casefold() in {u.casefold() for u in current_collabs} and eu not in to_remove:
                to_remove.append(eu)

        # Circuit breaker: trip on an absolute cap (--max-removals) or when the
        # removals would strip a MAJORITY of collaborators (wrong-roster case).
        # A single removal never trips it by itself.
        removal_count = len(to_remove)
        removal_pct = (removal_count / total_non_owner * 100.0) if total_non_owner else 0.0
        if to_remove and removal_count > max_removals:
            breaker_reason = "count"
        elif removal_count >= 2 and removal_pct > 50.0:
            breaker_reason = "majority"
        breaker_would_trip = breaker_reason is not None

        if args.dry_run:
            if breaker_would_trip and not allow_mass and human:
                console.print(
                    f"  [yellow]circuit breaker would trip[/yellow]: {removal_count} removal(s) "
                    f"({removal_pct:.0f}% of {total_non_owner} collaborator(s)) exceeds --max-removals "
                    f"{max_removals} or removes a majority"
                )
                console.print("  [dim]would require --allow-mass-removal (or a smaller change) to proceed[/dim]")
                console.print()
        elif to_remove and breaker_would_trip and not allow_mass:
            auto_confirm = bool(getattr(args, "yes", False)) or not human
            interactive = sys.stdin.isatty() and console.is_terminal
            if auto_confirm or not interactive:
                error(
                    f"refusing to remove {removal_count} of {total_non_owner} collaborator(s) "
                    f"({removal_pct:.0f}%): exceeds --max-removals {max_removals} or would remove a majority; "
                    f"re-run with --allow-mass-removal to proceed"
                )
                for u in to_remove:
                    removals.append((u, "blocked (circuit breaker)"))
                blocked_count = removal_count
                to_remove = []
                breaker_blocked = True
            elif not confirm_mass_removal(repo_full_name, removal_count, total_non_owner, removal_pct, max_removals):
                removals_declined = True
                for u in to_remove:
                    removals.append((u, "declined"))
                to_remove = []
            # else: user explicitly confirmed the mass removal — fall through and remove all of to_remove
        elif to_remove:
            # Normal-sized removal (or --allow-mass-removal set): existing confirm flow, unchanged.
            auto_confirm = bool(getattr(args, "yes", False)) or not human
            interactive = sys.stdin.isatty() and console.is_terminal
            if not auto_confirm and interactive and not confirm_removals(repo_full_name, len(to_remove)):
                removals_declined = True
                for u in to_remove:
                    removals.append((u, "declined"))
                to_remove = []

        if to_remove and human:
            console.print(f"  [yellow]removing {len(to_remove)} user(s)[/yellow]")
            console.print()

        for u in to_remove:
            if args.dry_run:
                removals.append((u, "would remove"))
                if human:
                    console.print(f"  [blue]○[/blue] {escape(u):<22} [dim]would remove[/dim]")
                continue

            r = _run(["gh", "api", "-X", "DELETE", f"repos/{repo_owner}/{repo_name}/collaborators/{u}"])

            if r.returncode == 0:
                removals.append((u, "removed"))
                removed += 1
                if human:
                    console.print(f"  [green]✓[/green] {escape(u):<22} [dim]removed[/dim]")
            else:
                removals.append((u, "remove failed"))
                remove_failed += 1
                if human:
                    console.print(f"  [red]✗[/red] {escape(u):<22} [red]remove failed[/red]")

        if human and (to_remove or removals_declined or breaker_blocked):
            console.print()
        if removals_declined and human:
            console.print("  [dim]removals skipped (not confirmed)[/dim]")
            console.print()
        if breaker_blocked and human:
            console.print("  [dim]removals blocked by circuit breaker (see error above)[/dim]")
            console.print()

    exit_code = 1 if (failed > 0 or remove_failed > 0 or blocked_count > 0) else 0

    # Machine-readable output
    json_out = getattr(args, "json_out", None)
    if json_mode or json_out:
        payload = {
            "mode": "dry-run" if args.dry_run else "apply",
            "repo": repo_full_name,
            "source": config.source,
            "results": [{"username": u, "name": display_names.get(u), "status": s, "detail": d} for u, s, d in results],
            "removals": [{"username": u, "status": s} for u, s in removals],
            "summary": {
                "invited": added,
                "updated": updated,
                "skipped": skipped,
                "failed": failed,
                "removed": removed,
                "remove_failed": remove_failed,
                "welcomed": welcomed,
                "removal_blocked": blocked_count,
            },
            "circuit_breaker": {
                "would_trip": breaker_would_trip,
                "reason": breaker_reason,
                "max_removals": max_removals,
                "removal_count": removal_count,
                "total_non_owner": total_non_owner,
                "removal_pct": round(removal_pct, 1),
                "allow_mass_removal": allow_mass,
            },
            "warnings": config.warnings,
            "run": _run_metadata(
                repo_full_name=repo_full_name,
                me=me,
                mode="dry-run" if args.dry_run else "apply",
                dry_run=args.dry_run,
                sync=args.sync,
            ),
        }
        rendered = json.dumps(payload, indent=2)
        if json_out:
            try:
                Path(json_out).expanduser().write_text(rendered + "\n", encoding="utf-8")
            except OSError as exc:
                error(f"could not write {json_out}: {exc}")
                return 1
        if json_mode:
            print(rendered)
            return exit_code

    # Human summary
    if human:
        print_separator()

        parts = []
        if args.dry_run:
            if added:
                parts.append(f"[blue]{added} would invite[/blue]")
            if updated:
                parts.append(f"[blue]{updated} would update[/blue]")
            would_remove = sum(1 for _, s in removals if s == "would remove")
            if would_remove:
                parts.append(f"[blue]{would_remove} would remove[/blue]")
        else:
            if added:
                parts.append(f"[green]{added} invited[/green]")
            if updated:
                parts.append(f"[yellow]{updated} updated[/yellow]")
        if skipped:
            parts.append(f"[dim]{skipped} skipped[/dim]")
        if failed:
            parts.append(f"[red]{failed} failed[/red]")
        if removed:
            parts.append(f"[yellow]{removed} removed[/yellow]")
        if remove_failed:
            parts.append(f"[red]{remove_failed} removals failed[/red]")
        if blocked_count:
            parts.append(f"[red]{blocked_count} removal(s) blocked[/red]")
        if welcomed:
            parts.append(f"[cyan]{welcomed} welcomed[/cyan]")

        summary = " · ".join(parts) if parts else "[dim]nothing to do[/dim]"
        console.print(f"  [bold]done[/bold]  {summary}")
        console.print()

        # Show AI summary at the end (useful for sharing via email/Slack)
        if ai_summary:
            if welcomed > 0:
                console.print("  [bold]Welcome message sent:[/bold]")
            else:
                console.print("  [bold]Repo summary (for sharing):[/bold]")
            console.print()
            for line in ai_summary.split("\n"):
                console.print(f"    {line}")
            console.print()

    return exit_code


# =============================================================================
# Directory report
# =============================================================================


def _handle_report(args: argparse.Namespace) -> int:
    """Handle --report: permission matrix for every repo in a directory."""
    incompatible = []
    if args.user:
        incompatible.append("--user")
    if args.sync:
        incompatible.append("--sync")
    if args.audit:
        incompatible.append("--audit")
    if args.dry_run:
        incompatible.append("--dry-run")
    if args.repo:
        incompatible.append("--repo")
    if args.from_repo:
        incompatible.append("--from")
    if args.source_override:
        incompatible.append("--file")
    if args.source != "team.yaml":
        incompatible.append("positional source")
    if getattr(args, "json_out", None):
        incompatible.append("--json-out")
    if incompatible:
        error(f"--report cannot be combined with: {', '.join(incompatible)}")
        return 2

    root = Path(args.report).expanduser()
    if not root.is_dir():
        error(f"not a directory: {escape(str(args.report))}")
        return 2

    if not shutil.which("gh"):
        error("GitHub CLI (gh) not found")
        err_console.print("  install: https://cli.github.com/")
        return 1

    human = not args.quiet and not args.json
    status = console.status(f"  Scanning repos in {root} ...", spinner="dots") if human else None
    if status:
        status.start()
    try:
        result = build_report(root, include_names=not args.no_names)
    finally:
        if status:
            status.stop()

    for failure in result.repo_failures:
        err_console.print(f"[yellow]warning:[/yellow] could not read collaborators for {failure}")

    if args.json:
        payload = {
            "root": str(root),
            "repos": result.repos,
            "rows": [
                {
                    "repo": r.repo,
                    "username": r.username,
                    "name": r.name or None,
                    "permission": r.permission,
                    "status": r.status,
                    "invited_at": r.invited_at or None,
                }
                for r in sorted(result.rows, key=lambda r: (r.repo.casefold(), r.username.casefold()))
            ],
            "summary": {
                "repos_scanned": result.repos_seen,
                "unique_users": len(result.usernames),
                "access_entries": len(result.rows),
                "dirs_skipped": result.dirs_skipped,
                "repo_failures": result.repo_failures,
            },
        }
        print(json.dumps(payload, indent=2))
    elif human:
        console.print()
        if not result.rows:
            console.print(f"  [dim]no repos with collaborators found under {root}[/dim]")
        else:
            header, lines = matrix_lines(result)
            table = Table(title=f"Access report: {root}", title_justify="left")
            table.add_column(header[0], style="bold", no_wrap=True)
            for col in header[1:]:
                table.add_column(col, justify="center", no_wrap=True)
            for line in lines:
                label, *cells = line
                style = "dim" if all(c == "·" for c in cells) else ""
                table.add_row(label, *cells, style=style)
            console.print(table)
            console.print()
            console.print(
                f"  [bold]{result.repos_seen}[/bold] repos · "
                f"[bold]{len(result.usernames)}[/bold] users · "
                f"[bold]{len(result.rows)}[/bold] access entries "
                f"[dim]({result.dirs_skipped} non-repo dirs skipped)[/dim]"
            )
            console.print()

    if args.csv:
        csv_path = Path(args.csv).expanduser()
        try:
            if args.format == "matrix":
                write_matrix_csv(result, csv_path)
            else:
                write_long_csv(result, csv_path)
        except OSError as exc:
            error(f"could not write {csv_path}: {exc}")
            return 1
        if human:
            console.print(f"  [green]✓[/green] wrote [bold]{csv_path}[/bold] [dim]({args.format} format)[/dim]")
            console.print()

    return 0


# =============================================================================
# Argument parsing
# =============================================================================


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="addteam",
        description="Collaborator management for GitHub repos.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  addteam                            # use local team.yaml
  addteam --from owner/config-repo   # use team.yaml from another repo
  addteam --from org/team-config --group maintainers   # one group only
  addteam -r owner/repo              # target a specific repo
  addteam -n                         # dry-run (preview)
  addteam -a                         # audit (show drift)
  addteam -a --fail-on-drift         # CI gate: exit 1 when drift found
  addteam -s                         # sync (also removes unlisted users)
  addteam -i                         # create starter team.yaml
  addteam -i --init-action           # also create GitHub Action
  addteam --json -a                  # machine-readable audit
  addteam --report ~/dev --csv out.csv   # audit all repos in a folder
""",
    )

    parser.add_argument(
        "source", nargs="?", default="team.yaml", help="Config source: local file or owner/repo (default: team.yaml)"
    )

    # Init commands (run before other args require gh)
    parser.add_argument("-i", "--init", action="store_true", help="Create starter team.yaml")
    parser.add_argument("--init-action", action="store_true", help="Create GitHub Action workflow")
    parser.add_argument("--init-multi-repo", action="store_true", help="Create multi-repo sync workflow")

    # Directory report
    parser.add_argument(
        "--report", metavar="DIR", help="Permission matrix for every repo found in DIR (directory audit)"
    )
    parser.add_argument("--csv", metavar="PATH", help="With --report: also write a CSV spreadsheet to PATH")
    parser.add_argument(
        "--format",
        choices=["long", "matrix"],
        default="long",
        help="With --report --csv: one row per repo+user (long) or users x repos grid (matrix)",
    )
    parser.add_argument("--no-names", action="store_true", help="With --report: skip display-name lookups (faster)")

    # Config source options
    parser.add_argument(
        "-f", "--file", metavar="PATH", dest="source_override", help="Config source (alternative to positional arg)"
    )
    parser.add_argument(
        "--from",
        metavar="OWNER/REPO",
        dest="from_repo",
        help="Fetch team.yaml from another repo (explicit spelling of positional owner/repo)",
    )
    parser.add_argument(
        "--group",
        action="append",
        metavar="ROLE",
        help="Only apply these role groups (e.g. --group maintainers; repeatable). Never combinable with --sync",
    )

    # What to change
    parser.add_argument("-u", "--user", metavar="NAME", help="Invite a single GitHub user")
    parser.add_argument(
        "-p", "--permission", default="push", choices=sorted(VALID_PERMISSIONS), help="Permission (default: push)"
    )
    parser.add_argument("-r", "--repo", metavar="OWNER/REPO", help="Target repo (default: current directory)")

    # Modes
    parser.add_argument("-n", "--dry-run", action="store_true", help="Preview without making changes")
    parser.add_argument("-s", "--sync", action="store_true", help="Remove collaborators not in list")
    parser.add_argument(
        "--max-removals",
        type=int,
        default=3,
        metavar="N",
        help="With --sync: abort (or prompt harder) if planned removals exceed N or a majority of current collaborators (default: 3)",
    )
    parser.add_argument(
        "--allow-mass-removal",
        action="store_true",
        help="With --sync: allow removals that exceed --max-removals or would remove a majority of collaborators",
    )
    parser.add_argument("-a", "--audit", action="store_true", help="Show drift without making changes")
    parser.add_argument(
        "--fail-on-drift", action="store_true", help="With --audit: exit 1 when drift is found (for CI gates)"
    )
    parser.add_argument(
        "--map-down",
        action="store_true",
        help="No-op since 1.4.0: personal repos degrade automatically (kept for compatibility)",
    )

    # Output / behavior
    parser.add_argument("--json", action="store_true", help="Machine-readable JSON output (audit/apply)")
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        default=None,
        help="Write the run payload to PATH (independent of --json/stdout); apply/audit/dry-run only",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts (e.g. sync removals)")
    parser.add_argument("-q", "--quiet", action="store_true", help="Minimal output")
    parser.add_argument(
        "--welcome",
        action="store_const",
        const=True,
        default=None,
        help="Enable welcome issues for this run (default is off unless welcome_issue: true is set in config)",
    )
    parser.add_argument("--no-welcome", action="store_true", help="Skip creating welcome issues")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI-generated summary")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "openai", "anthropic", "google", "openrouter"],
        help="AI provider (default: auto)",
    )
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _resolve_welcome(args: argparse.Namespace, config: TeamConfig) -> bool:
    """Resolve welcome-issue behavior: flags override config; default is OFF."""
    if args.no_welcome:
        return False
    if args.welcome:
        return True
    if config.welcome_issue is not None:
        return bool(config.welcome_issue)
    return False


def run(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.init or args.init_action or args.init_multi_repo:
        return _handle_init(args)

    if args.report is not None:
        return _handle_report(args)

    # ==========================================================================
    # VALIDATION
    # ==========================================================================

    if args.repo and not _is_valid_repo_spec(args.repo):
        error(f"invalid repo: {escape(args.repo)}")
        return 2

    if args.user and args.sync:
        error("--sync cannot be used with --user")
        return 2

    if args.group and args.sync:
        error("--group cannot be used with --sync (a filtered subset is never the full source of truth)")
        return 2

    if args.group and args.user:
        error("--group cannot be used with --user")
        return 2

    if args.max_removals < 0:
        error("--max-removals cannot be negative")
        return 2

    if args.group:
        unknown_groups = set(args.group) - set(ROLE_PERMISSIONS) - {"collaborators"}
        if unknown_groups:
            error(f"unknown group(s): {', '.join(sorted(unknown_groups))}")
            err_console.print(f"  valid groups: {', '.join(sorted(set(ROLE_PERMISSIONS) | {'collaborators'}))}")
            return 2

    # Mutually exclusive config sources
    explicit_sources = [
        s
        for s in (
            args.source if args.source != "team.yaml" else None,
            args.source_override,
            args.from_repo,
        )
        if s
    ]
    if len(explicit_sources) > 1:
        error("conflicting config sources: use only one of positional, -f/--file, or --from")
        return 2

    if args.from_repo and not _is_valid_repo_spec(args.from_repo):
        error(f"invalid --from repo: {escape(args.from_repo)}")
        return 2

    if not shutil.which("gh"):
        error("GitHub CLI (gh) not found")
        err_console.print("  install: https://cli.github.com/")
        return 1

    # ==========================================================================
    # RESOLVE REPO
    # ==========================================================================

    view_args = ["repo", "view"]
    if args.repo:
        view_args.append(args.repo)
    view_args.extend(["--json", "name,owner,description,isInOrganization"])

    try:
        repo = _gh_json(view_args, what="resolve repo")
    except RuntimeError as exc:
        if not args.repo and "not a git repository" in str(exc):
            error("not inside a git repository, and no -r target given")
            err_console.print()
            err_console.print("  [dim]Run inside a repo, or target one explicitly:[/dim]  addteam -r owner/repo")
            err_console.print("  [dim]Audit a whole folder of repos:[/dim]               addteam --report DIR")
            err_console.print()
        else:
            error(str(exc))
        return 1

    if not isinstance(repo, dict):
        error("unexpected response format from gh repo view")
        return 1

    repo_name = repo["name"]
    repo_owner = repo["owner"]["login"]
    description = repo.get("description") or ""

    try:
        me = _gh_text(["api", "user", "--jq", ".login"], what="resolve authenticated user")
    except RuntimeError as exc:
        error(str(exc))
        return 1

    repo_full_name = f"{repo_owner}/{repo_name}"

    mode = None
    if args.dry_run:
        mode = "dry-run"
    elif args.audit:
        mode = "audit"

    show_ui = not args.quiet and not args.json
    if show_ui:
        print_header(repo_name, repo_owner, me, mode)
        check_for_updates()

    is_personal_repo = repo.get("isInOrganization") is False

    # ==========================================================================
    # LOAD CONFIG
    # ==========================================================================

    if args.user:
        u = args.user.lstrip("@").strip()
        config = TeamConfig(
            collaborators=[Collaborator(u, args.permission)] if u else [],
            source=f"--user {u}",
        )
    else:
        config_source = args.from_repo or args.source_override or args.source
        try:
            config, _ = _resolve_team_config(config_source, repo_owner, repo_name)
        except FileNotFoundError:
            err_console.print()
            err_console.print("  [yellow]No team config found.[/yellow]")
            err_console.print()
            err_console.print("  [dim]Create one:[/dim]            addteam --init")
            err_console.print("  [dim]Use another repo:[/dim]      addteam --from owner/config-repo")
            err_console.print("  [dim]Point at a file:[/dim]       addteam -f path/to/team.yaml")
            err_console.print()
            return 1
        except (ValueError, TypeError, RuntimeError) as exc:
            error(str(exc))
            return 1

    config.welcome_issue = _resolve_welcome(args, config)

    # --group: narrow the config to members of the selected role groups.
    # Highest permission still wins for people in multiple groups.
    if args.group:
        selected = set(args.group)
        config.collaborators = [c for c in config.collaborators if c.groups & selected]
        if not config.collaborators:
            error(f"no collaborators found in group(s): {', '.join(sorted(selected))}")
            return 1

    if not config.collaborators:
        if show_ui:
            console.print("  [dim]no collaborators found[/dim]")
        if args.sync:
            error("cannot sync with empty list")
            return 2
        return 0

    # Never sync against a partially-resolved config: a failed team lookup
    # would make every member of that team look "unlisted" and get removed.
    if args.sync and config.incomplete:
        error("config could not be fully resolved (see warnings above) — refusing to --sync against partial state")
        error("fix the problem, or use --audit to inspect what was resolved")
        return 1

    # Personal (non-org) repos only allow pull/push/admin — GitHub 422s on
    # maintain/triage invitations and silently ignores such updates. Degrade
    # automatically (mapping down never expands access, only shrinks it) and
    # say so loudly, grouped into one line per mapping. Org repos are unaffected.
    if is_personal_repo:
        degraded: dict[tuple[str, str], list[str]] = {}
        for c in config.collaborators:
            if c.is_expired or c.username in (repo_owner, me) or c.permission not in ("maintain", "triage"):
                continue
            lowered = "push" if c.permission == "maintain" else "pull"
            degraded.setdefault((c.permission, lowered), []).append(c.username)
            c.permission = lowered
        for (src, dst), users in sorted(degraded.items()):
            warning(f"personal repo: degraded {src} → {dst} (org-repo level): {', '.join(sorted(users))}")

    if show_ui:
        default_perm = args.permission if args.user else config.default_permission
        print_config(
            config.source,
            repo_full_name,
            default_perm,
            args.sync,
            len(config.collaborators),
            bool(config.welcome_issue),
            config.warnings,
            args.group,
        )

    if args.audit:
        return _handle_audit(config, repo_owner, repo_name, me, args)

    return _handle_apply(args, config, repo_owner, repo_name, repo_full_name, description, me)
