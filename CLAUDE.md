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

The entire application logic lives in `src/addteam/bootstrap_repo.py` (~1500 lines). The `cli.py` is just a thin entry point that calls `run()`.

### Key Components in bootstrap_repo.py

**Data Models** (lines 183-215):
- `Collaborator`: User with permission, optional expiry date, and team membership
- `TeamConfig`: Parsed YAML config with collaborator list and settings
- `AuditResult`: Drift detection results (missing, extra, permission changes, expired)

**Config Resolution** (lines 721-803):
- `_resolve_team_config()`: Main entry point for loading config
- Supports: local files, remote repo files (`owner/repo` fetches team.yaml from that repo), `repo:` prefix for target repo files, `local:` prefix for explicit local paths
- Auto-detects YAML vs plain text format

**GitHub API Interactions** (lines 333-510):
- All GitHub operations go through `gh` CLI (not direct API calls)
- `_gh_json()` and `_gh_text()` are the core helpers
- Handles collaborators, invitations, team members, repo info, and welcome issues

**AI Summary Generation** (lines 865-1012):
- Supports OpenAI, Anthropic, Google, and OpenRouter
- Auto-selects provider based on available API keys
- Used for welcome issues and end-of-run summaries

### CLI Modes

The `run()` function (line 1070+) handles three main modes:
1. **Init mode**: Creates team.yaml and/or GitHub Actions workflow
2. **Audit mode** (`-a`): Shows drift without making changes
3. **Apply mode** (default): Invites/removes collaborators, creates welcome issues

### Permission Mapping

Role names in YAML map to GitHub permissions:
- `admins` → admin
- `maintainers` → maintain
- `developers`, `contributors` → push
- `reviewers`, `readers` → pull
- `triagers` → triage
