# addteam

One-command collaborator management for GitHub repos.

## Quick Start

```bash
# Inside your repo
uvx git+https://github.com/michaeljabbour/addteam@main

# Or target any repo
uvx git+https://github.com/michaeljabbour/addteam@main -r owner/repo
```

**Prerequisites:** [GitHub CLI](https://cli.github.com/) installed and authenticated.

## What It Does

1. Reads usernames from `collaborators.txt`
2. Invites them as collaborators with specified permissions
3. Optionally generates an AI summary of your repo

## Collaborators File

Create `collaborators.txt` in your repo root:

```
# Team members (one per line)
alice
bob
@charlie  # @ prefix is optional
```

### File Resolution Order

1. Local file in current directory or repo root
2. File in the target GitHub repo
3. Fallback to `michaeljabbour/addteam` repo

## Usage

```bash
addteam [options]
```

### Common Options

| Short | Long | Description |
|-------|------|-------------|
| `-n` | `--dry-run` | Preview without making changes |
| `-r` | `--repo OWNER/REPO` | Target a specific repo |
| `-u` | `--user NAME` | Invite a single user |
| `-f` | `--collaborators-file FILE` | Use custom file |
| `-p` | `--permission LEVEL` | Permission: pull, triage, push (default), maintain, admin |
| `-s` | `--sync` | Remove collaborators not in list |
| `-q` | `--quiet` | Minimal output |
| | `--no-ai` | Skip AI summary |
| | `--write-readme` | Write summary to README.md |

### Examples

```bash
# Preview what would happen
addteam -n

# Invite a single user
addteam -u octocat

# Use a different collaborators file
addteam -f team-prod.txt

# Sync mode: add missing, remove unlisted
addteam -s -n  # preview first!

# Target a specific repo from anywhere
addteam -r myorg/myrepo

# Force reading from repo (not local)
addteam -f repo:collaborators.txt

# Force reading local file
addteam -f local:./team.txt
```

## Installation Options

### One-liner (no install)

```bash
uvx git+https://github.com/michaeljabbour/addteam@main
```

### Install globally

```bash
uv tool install git+https://github.com/michaeljabbour/addteam@main
addteam --help
```

### From source

```bash
git clone https://github.com/michaeljabbour/addteam
cd addteam
uv run addteam
```

## AI Summary

If `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is set, generates a brief repo description after inviting collaborators.

- Tries OpenAI first, falls back to Anthropic
- Use `--no-ai` to skip
- Use `--write-readme` to inject into README.md between markers:
  ```
  <!-- BEGIN AUTO SUMMARY -->
  ...generated content...
  <!-- END AUTO SUMMARY -->
  ```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for summaries |
| `ANTHROPIC_API_KEY` | Anthropic API key for summaries |
| `ADDMADETEAM_FALLBACK_COLLABORATORS_REPO` | Custom fallback repo (default: michaeljabbour/addteam) |

## Output Example

```
addteam v0.1.0

  amplifier-dx (michaeljabbour)
  authenticated as michaeljabbour

  source      michaeljabbour/addteam:collaborators.txt (fallback)
  permission  push
  users       15

  ✓ alice                invited
  ✓ bob                  invited
  · michaeljabbour       owner
  ✓ charlie              invited
  ...

  ──────────────────────────────────────────────────

  done  14 invited · 1 skipped

  ╭─ repo summary ─────────────────────────────────╮
  │                                                │
  │  Quick start:                                  │
  │    uvx git+https://github.com/...@main        │
  │    Run inside the repo you want to manage.    │
  │    Prereqs: gh installed + authenticated.     │
  │                                                │
  │  This tool bootstraps collaborator access...  │
  │                                                │
  ╰────────────────────────────────────────────────╯
```

## License

MIT
