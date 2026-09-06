# Changelog

All notable changes to this project will be documented in this file.

## [1.6.0] - 2026-09-06

### Added
- **Expired-invitation awareness** — GitHub auto-expires a pending invite after 7 days; addteam now detects this separately from config `expires:`. Apply automatically deletes-and-resends an expired invite (reported as `re-invited (expired)`); audit shows `pending (Nd)` or `expired`; `--report`/`--org`/`--repos` gain an `invited_at` column and an `expired` status, and the terminal matrix marks pending `*` / expired `!`.
- **`--max-removals N` / `--allow-mass-removal`** — a circuit breaker on `--sync`: removals exceeding N (default 3) or a majority (>50%) of current collaborators are blocked when non-interactive (exit 1), or require an explicit confirm with a loud warning when interactive; a single removal never trips it. Only *unlisted* removals count toward the breaker — removals authored as `expires:` are explicit intent: they're still removed (normal confirm rules) even when the breaker blocks unlisted removals, and they're reported separately as `expired_removals` in the `circuit_breaker` JSON block. `--dry-run` reports whether the breaker would trip.
- **`--json-out PATH`** — write the apply/audit/dry-run payload to a file (independent of `--json`/stdout), including a new `run` metadata block (version, timestamp, actor, repo, mode, dry_run, sync) and, for sync runs, a `circuit_breaker` block with the trip decision and reason. Rejected (exit 2) when combined with `--report`.
- **GitHub Action scaffold**: the sync workflow now uploads a run-report artifact and posts a step summary; pull requests touching `team.yaml` get a new `plan` job that dry-runs `--sync` and posts/updates a PR comment with the plan. Multi-repo scaffold writes one JSON artifact per repo and fails the job at the end if any repo failed (every repo is still processed, with a `::error::` annotation per failure) instead of swallowing failures.
- **`addteam --init --from-current`** — generate team.yaml from the repo's actual current collaborators and pending invitations (grouped by permission into role buckets), instead of the blank starter template. Pending invites are annotated `# pending invite`; EXPIRED pending invites are written commented out (`# - user  # expired invite — uncomment to re-invite`) so the snapshot matches current effective access. `welcome_issue: false` is set explicitly, and running `--sync` immediately after is a true no-op.
- **`addteam accept`** — list and accept your own pending repository invitations across repos (the invitee side, as opposed to everything else addteam does from the repo-admin side). `--from OWNER` filters by inviting org/owner; `--dry-run`, `--json`, `-y/--yes`, `-q/--quiet` all work as elsewhere. Expired invitations are listed but not acceptable — addteam tells you to ask the inviter to re-run `addteam`.
- **`--org NAME`** and **`--repos PATH`** — permission-matrix reporting sourced from a GitHub org's repo list or an explicit `owner/repo` list file, alongside the existing `--report DIR`. Forks are skipped by default (`--include-forks` to keep them); archived repos are included and flagged. New row fields: `visibility` (public/private/internal) and `fork` (bool), populated for `--org`/`--repos`.
- **Org team membership management** — `teams:` entries accept a mapping form (`org/slug: {permission, members, maintainers}`) alongside the existing scalar form. Apply ensures listed users are members/maintainers of the team itself (not just repo collaborators); audit reports missing/extra members and role mismatches. Removing extra team members requires the new `--sync-teams` flag (never implied by plain `--sync`) and is subject to the same circuit breaker as repo-collaborator removal, computed per team. A desired maintainer who is currently a plain member is reported exactly once, as a role mismatch, and fixed with a single role update. A team whose membership can't be read (403/404) is treated as incomplete for that team only — no removals attempted, clear "requires org admin or team maintainer" messaging. Skipped entirely on personal repos. Combining `--group` with `--sync-teams` is rejected (exit 2), same as `--group` with `--sync`.

### Changed
- **Welcome issues are now opt-in** — default flips from on to off. Enable with `welcome_issue: true` in team.yaml, or `--welcome` for a single run. `--no-welcome` still always forces them off. No AI provider is contacted (no API-key warnings either) unless welcome issues are actually active.
- Terminal `--report`/`--org`/`--repos` matrix now shows a single-letter permission initial per cell (was the full word) to stay compact with many repos; pending (`*`) and expired (`!`) markers unchanged.

## [1.5.0] - 2026-09-04

### Changed (UX polish)
- **No more AI spend on no-op runs**: the summary/welcome-message generation now happens only when someone will actually be (re-)invited — converged repos don't call any provider or print a "Repo summary" block
- **Grouped degradation warnings**: one line per permission mapping (`maintain → push: alex, casey, sam`) instead of one per user
- **Compact header**: drops the redundant `authenticated as` line when you are the repo owner
- **Display-name merge on dedupe**: a user listed in multiple groups keeps the highest permission AND picks up their `name:` from whichever group has it (previously first-seen dropped later names)

## [1.4.0] - 2026-09-04

### Changed
- **Personal-repo degradation is now automatic** — no flags needed: maintain → push and triage → pull on user-owned repos, with a per-user warning. One central `team.yaml` covers org and personal repos alike. The preflight error from 1.3.1 is gone; `--map-down` remains as a no-op so existing commands don't break

## [1.3.2] - 2026-09-04

### Added
- **`--map-down`**: on personal repos, degrade `maintain → push` and `triage → pull` (with per-user warnings) instead of failing — lets one central config cover both org and personal repos. Org repos are unaffected

## [1.3.1] - 2026-09-04

### Fixed
- **Personal-repo permission limits**: GitHub only allows pull/push/admin in collaborator invitations on user-owned repos. Previously `maintain`/`triage` invites failed mid-run with raw 422s, and in-place updates were silently ignored (2xx, no change). Apply now fails fast with an actionable message (who's affected, map maintain → push / triage → pull, or use an org repo); dry-run/audit warn instead
- **Silent permission updates detected**: in-place updates are verified with a follow-up permission read; a no-op now reports "update ignored by GitHub (still X)"
- **Stale pending invitations fixed automatically**: a pending invite whose permission differs from the config can no longer wedge a repo — GitHub can't edit invitations, so apply deletes and re-invites (dry-run previews as "stuck at X")

### Changed
- `--audit`/`--json` pending-invite payloads keep working; `_get_pending_invitations` now returns `login -> {id, permission}` (plain sets still accepted)

## [1.3.0] - 2026-09-04

### Added
- **`--group ROLE`** (repeatable): narrow a config to selected role groups — e.g. `addteam --from michaeljabbour/madeteam --group maintainers` applies just the leads. People in multiple groups keep their highest permission; group membership is tracked per user during parsing. Never combinable with `--sync` (a filtered subset is not the full source of truth) or `--user`

## [1.2.1] - 2026-09-04

### Fixed
- Running outside a git repository without `-r` now prints actionable guidance (`-r owner/repo`, `--report DIR`) instead of a raw `fatal: not a git repository`

## [1.2.0] - 2026-09-04

### Added
- **`name:` display names in team.yaml** — `username: dluc` + `name: Devis Lucato` entries show real names in audit/apply output and --json, and address welcome issues by name. Backwards compatible: `name:` alone still acts as the username alias
- **`--report DIR`** — directory-wide access audit: scans every git repo in a folder, builds a users x repos permission matrix in the terminal (active + pending invitations), looks up display names from GitHub profiles
- **`--csv PATH`** with `--format long|matrix` — export the report as a spreadsheet; `--no-names` to skip profile lookups; report also supports `--json`
- ruff pinned (`>=0.16,<0.17`) — `uv.lock` is not committed, so CI and local must share a ruleset

### Fixed
- Lint compatibility with ruff 0.16 (narrowed exception handling, explicit subprocess `check=`, etc.)

## [1.1.0] - 2026-09-04

UX/ergonomics overhaul: module split, correctness fixes, scripting support.

### Added
- `--from owner/repo`: explicit spelling for "use team.yaml from another repo" (avoids confusion with `-r owner/repo`, the *target* repo). Conflicting config sources (positional + `-f` + `--from`) are now rejected with a usage error
- `--json`: machine-readable output for audit and apply modes
- `--fail-on-drift`: audit exits 1 when drift is found (CI gates)
- `--yes`: skip interactive confirmation for `--sync` removals (tty-only prompt otherwise)
- `--welcome`: force welcome issues on; `welcome_issue: false` in config is now honored (default stays on)
- `contractors` role group (used by the init template since 0.3.0, now actually parsed)
- Unknown top-level YAML keys produce warnings instead of being silently ignored
- Permissions are updated in place when a collaborator's access drifts from team.yaml
- Sync refuses to run when the config is partially resolved (e.g. a team lookup failed), preventing mass removals after API errors
- Update check is cached for 24h, skipped in CI, and opt-out via `ADDTEAM_NO_UPDATE_CHECK`

### Fixed
- Relative paths containing `/` (e.g. `examples/team.yaml`) were misread as remote `owner/repo` config sources and reported "No team.yaml found"
- Permission level never rendered in apply/dry-run output (`invite [push]` was parsed as Rich markup)
- Repos with >100 collaborators/invitations crashed: paginated `gh api` output needs `--slurp` to be parseable JSON
- Missing config exited 0, hiding failures from CI/scripts (now 1)
- `--version` disagreed with package metadata (0.9.0 vs 1.0.0); version is now single-sourced from `importlib.metadata`
- Audit mode reported users with pending invitations as "Missing"
- Errors and warnings now go to stderr (stdout stays clean for piping)
- Failed removals did not affect the exit code

### Changed
- **Architecture**: `bootstrap_repo.py` (1,656 lines) split into `models`, `config`, `gh`, `ai`, `ui`, `app`; the old module remains as a backwards-compatible import shim
- `-h/--help` epilog regrouped with more examples
- Removed the `_normalize_argv` shim for concatenated long flags (non-standard; standard argparse forms all work)
- Coverage up from 73% to 80%, 139 tests

## [1.0.0] - 2026-02-23

### Added
- **103 tests** — comprehensive test suite covering all major code paths
  - Sync removal path (the most dangerous `--sync` DELETE logic)
  - Cascading config resolution (`_resolve_team_config`)
  - Welcome issue creation and body assembly
  - `--init` / `--init-action` / `--init-multi-repo` scaffolding
  - Audit output formatting and drift detection
- **Coverage gate** — CI enforces ≥70% coverage via pytest-cov (currently 73%)
- **Type checking** — pyright added to dev dependencies and CI
- **Format checking** — `ruff format --check` added to CI

### Fixed
- **10 type errors** resolved across the codebase
  - `_http_post_json`: `resp` was possibly unbound in `JSONDecodeError` handler — restructured to separate try blocks
  - `_gh_json` callers: return type `dict | list` was used as `dict` without narrowing — added runtime type guards at both call sites
- **CI Python matrix** now includes 3.10 (matches `requires-python = ">=3.10"`)

### Changed
- CI split into `lint` and `test` jobs (lint runs once, tests run across matrix)
- `publish` job now depends on both `lint` and `test`
- Dev dependencies updated: added `pytest-cov>=4.0` and `pyright>=1.1`
- Development status promoted from Beta to stable

## [0.8.5] - 2024-12-18

### Changed
- Improved AI summary prompt with structured output format
- Summary now includes: repo name, URL, what it does, install, quick start
- Prefers uvx/pipx over pip install
- No emojis, no markdown, no fluff

## [0.8.4] - 2024-12-18

### Changed
- AI summary now always displays (useful for sharing via email/Slack)
- Shows "Repo summary (for sharing)" when no new invites sent

## [0.8.3] - 2024-12-18

### Changed
- Friendlier first-run experience when no team.yaml found
- Shows helpful guidance instead of error message
- Exit code 0 (not an error, just needs setup)

## [0.8.2] - 2024-12-18

### Changed
- AI summary now displayed at END of run (after invites complete)
- AI prompt requests plain text output (no markdown formatting)
- Cleaner terminal output for welcome messages

## [0.8.1] - 2024-12-18

### Changed
- Clearer status messages for collaborator states:
  - "already has access" - user accepted invitation
  - "already invited" - invitation pending acceptance
  - "invited" - newly invited

## [0.8.0] - 2024-12-18

### Added
- Skip existing collaborators - no duplicate invites or welcome issues
- Display AI welcome summary in terminal before sending

### Changed
### Changed
- Users already on repo show "already push" instead of being re-invited


### Added
- AI provider status feedback (shows which provider is used or why none available)

## [0.7.1] - 2024-12-18

### Fixed
- Update notification now shows uvx refresh hint

## [0.7.0] - 2024-12-18

### Changed
- **Welcome issues ON by default**: AI-powered welcome issues are now created automatically
  - Use `--no-welcome` to disable
  - Use `--no-ai` to skip AI summary but still create basic welcome issues

### Added
- **Google Gemini support**: Added `GOOGLE_API_KEY` as third fallback for AI summaries
- **OpenRouter support**: Added `OPENROUTER_API_KEY` as fourth fallback
- AI provider priority: OpenAI → Anthropic → Google → OpenRouter
- `--provider` flag now accepts: `auto`, `openai`, `anthropic`, `google`, `openrouter`

## [0.6.0] - 2024-12-18

### Added
- **Auto-update check**: Notifies users when a newer version is available on PyPI
- Check runs on each invocation (2s timeout, fails silently)
- Shows: `update available: 0.6.0 → 0.7.0  (pip install -U addteam)`

## [0.5.1] - 2024-12-18

### Improved
- **Enhanced welcome issues**: Now include repo metadata, language-specific setup hints, and contextual onboarding
- **Smarter AI summaries**: AI now reads README content to generate accurate install/usage instructions
- Increased AI max_tokens from 200 to 500 for richer welcome messages

### Added
- `_get_repo_info()`: Fetches repo description, topics, language, and homepage
- `_get_readme_excerpt()`: Fetches README content for AI context
- Language-specific setup hints (Python, JavaScript, TypeScript, Rust, Go)
- Topics display in welcome issues
- Direct links to README and homepage

## [0.5.0] - 2024-12-18

### Changed
- **Simplified CLI**: Config source is now a positional argument
  - Before: `addteam -f owner/repo`
  - After: `addteam owner/repo`
- `-f` flag kept for backwards compatibility

### Added
- `py.typed` marker for type checking support
- `CONTRIBUTING.md` guide

## [0.4.0] - 2024-12-18

### Changed
- **Breaking**: Renamed internal package from `addmadeteam` to `addteam`
- Adopted `src/` layout (modern Python packaging best practice)
- Clean git history (no traces of original internal name)

### Added
- Remote config fetch: `-f owner/repo` fetches team.yaml from another GitHub repo
- Examples directory with sample configurations

## [0.3.1] - 2024-12-18

### Added
- Remote repo support for `-f` flag
- PyPI package publication
- Comprehensive README with GitOps documentation

### Fixed
- License format for modern setuptools compatibility

## [0.3.0] - 2024-12-18

### Added
- GitOps workflow with `--init-action`
- Multi-repo management with `--init-multi-repo`
- Audit mode (`-a`) for drift detection
- Welcome issues with AI-generated summaries
- Expiring access support

## [0.2.0] - 2024-12-18

### Added
- Role-based permission inference (admins → admin, developers → push, etc.)
- GitHub Teams integration
- YAML configuration format

## [0.1.0] - 2024-12-18

### Added
- Initial release
- Basic collaborator management
- Dry-run mode
- Sync mode (add/remove)
