# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

addteam is a CLI tool for managing GitHub repository collaborators via YAML configuration. It uses the GitHub CLI (`gh`) under the hood and supports GitOps workflows, expiring access, GitHub team integration, and AI-generated welcome messages.

## Development Commands

```bash
# Install dependencies
uv sync --all-extras

# Run the CLI
uv run addteam --help

# Run tests
uv run pytest

# Run single test
uv run pytest tests/test_bootstrap.py::TestCollaborator::test_not_expired_when_no_date

# Lint
uv run ruff check src/
uv run ruff format src/
```

## Architecture

The application is split into small focused modules under `src/addteam/`:

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | `__version__` single-sourced from package metadata |
| `models.py` | `Collaborator`, `TeamConfig`, `AuditResult`, permission/role maps |
| `templates.py` | `team.yaml` + GitHub Actions templates for `--init*` |
| `console.py` | stdout/stderr Rich consoles; `error()`/`warning()` helpers (stderr) |
| `gh.py` | All GitHub interactions via the `gh` CLI (`_run_checked`, `_gh_json`, paginated fetches with `--slurp`, team members, welcome issues) |
| `ai.py` | Provider map + summary generation (OpenAI, Anthropic, Google, OpenRouter; auto-select by API key) |
| `config.py` | Path helpers, YAML/text parsing, cascading `_resolve_team_config()` |
| `ui.py` | Header/config rendering, cached PyPI update check, removal confirmation |
| `report.py` | `--report DIR` directory audit: repo discovery, access collection, CSV/matrix emitters |
| `app.py` | Argparse (`run()`), init/audit/apply/report handlers, `--json` emission |
| `cli.py` | Thin entry point: `SystemExit(run())` |
| `bootstrap_repo.py` | Backwards-compat shim re-exporting the old import surface (used by `scripts/bootstrap_repo.py`) |

### Key Behaviors

**Config Resolution** (`config._resolve_team_config()`), in order:
1. Explicit prefixes: `local:path`, `repo:path`
2. An existing local file wins — a nested relative path (`examples/team.yaml`) is never mistaken for `owner/repo`
3. `owner/repo` — fetch team.yaml/team.yml from that repo
4. Default filenames locally (`team.yaml`, `team.yml`, `collaborators.*`), then in the target repo

Unknown YAML keys land in `TeamConfig.warnings`; team expansion failures also set `TeamConfig.incomplete`, which makes `--sync` refuse to run (mass-removal guard).

**GitHub API Interactions**: everything goes through the `gh` CLI (not direct API calls). Paginated list endpoints use `--paginate --slurp` via `_gh_api_paginated()`.

### CLI Modes

`run()` in `app.py` handles four modes:
1. **Init mode**: Creates team.yaml and/or GitHub Actions workflow
2. **Report mode** (`--report DIR`): users × repos permission matrix for every git repo in DIR; `--csv` (long/matrix), `--json`, `--no-names`
3. **Audit mode** (`-a`): Shows drift without changes; `--fail-on-drift` exits 1 for CI; `--json` for machines
4. **Apply mode** (default): Invites/removes collaborators, fixes permission drift in place, creates welcome issues; removal asks for tty confirmation unless `--yes`

Exit codes: 0 success, 1 runtime error (or drift with `--fail-on-drift`), 2 usage error.

### Permission Mapping

Role names in YAML map to GitHub permissions:
- `admins` → admin
- `maintainers` → maintain
- `developers`, `contributors`, `contractors` → push
- `reviewers`, `readers` → pull
- `triagers` → triage
