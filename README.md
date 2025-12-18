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

1. Reads team config from `team.yaml` (or `collaborators.txt`)
2. Invites collaborators with role-based permissions
3. Optionally creates welcome issues with AI-generated repo summaries
4. Supports expiring access and audit mode

## Team Configuration

Create `team.yaml` in your repo root:

```yaml
# Team configuration
default_permission: push
welcome_issue: true  # Auto-create welcome issues

# Role-based groups (permission inferred from role name)
admins:
  - alice

developers:
  - bob
  - charlie

reviewers:
  - eve

# Temporary access with expiry dates
contractors:
  - username: temp-dev
    permission: push
    expires: 2025-06-01

# GitHub team integration (for orgs)
teams:
  - myorg/backend-team
  - myorg/frontend-team: pull
```

### Supported Roles

| Role | Permission |
|------|------------|
| `admins` | admin |
| `maintainers` | maintain |
| `developers`, `contributors` | push |
| `reviewers`, `readers` | pull |
| `triagers` | triage |

### File Resolution Order

1. Local `team.yaml` or `collaborators.txt`
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
| `-f` | `--file FILE` | Use custom config file |
| `-p` | `--permission LEVEL` | Permission: pull, triage, push, maintain, admin |
| `-s` | `--sync` | Remove collaborators not in list |
| `-a` | `--audit` | Show drift without making changes |
| `-w` | `--welcome` | Create welcome issues for new users |
| `-q` | `--quiet` | Minimal output |
| | `--no-ai` | Skip AI summary |
| | `--write-readme` | Write summary to README.md |

### Examples

```bash
# Preview what would happen
addteam -n

# Audit mode: see who has access vs who should
addteam -a

# Sync mode: add missing, remove unlisted (preview first!)
addteam -s -n

# Invite with welcome issue
addteam -w

# Target a specific repo from anywhere
addteam -r myorg/myrepo

# Use a different config file
addteam -f team-prod.yaml
```

## Audit Mode

See drift between desired state (config) and actual state (GitHub):

```bash
addteam --audit
```

Output:
```
  ⚠ drift detected

  Missing (should have access):
    + alice (push)
    + bob (push) from myorg/dev-team

  Extra (should not have access):
    - mallory

  Permission drift:
    ~ charlie: admin → push

  Expired (should be removed):
    ⏰ contractor (expired 2025-01-15)

  total drift: 4 item(s)
```

## Welcome Issues

When `--welcome` is enabled (or `welcome_issue: true` in YAML), new collaborators automatically get a welcome issue with:

- AI-generated repo summary
- Quick start command
- Getting started checklist

## Expiring Access

Set expiry dates for temporary collaborators:

```yaml
contractors:
  - username: temp-dev
    permission: push
    expires: 2025-06-01  # ISO date format
```

- Expired users are skipped during invite
- With `--sync`, expired users are automatically removed
- Audit mode shows expired access

## GitHub Teams Integration

For organizations, sync with GitHub teams:

```yaml
teams:
  - myorg/backend-team           # uses default_permission
  - myorg/frontend-team: pull    # explicit permission
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

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key for AI summaries |
| `ANTHROPIC_API_KEY` | Anthropic API key for AI summaries |
| `ADDMADETEAM_FALLBACK_COLLABORATORS_REPO` | Custom fallback repo |

## Output Example

```
addteam v0.2.0

  amplifier-dx (michaeljabbour)
  authenticated as michaeljabbour

  source      local:team.yaml
  permission  push
  welcome     create issues for new users
  users       15

  ✓ alice                invited [push]
  ✓ bob                  invited [push]
  · michaeljabbour       owner
  ...

  ──────────────────────────────────────────────────

  done  14 invited · 1 skipped · 14 welcomed

  ╭─ repo summary ────────────────────────────────╮
  │                                               │
  │  Quick start:                                 │
  │    uvx git+https://github.com/...@main       │
  │                                               │
  ╰───────────────────────────────────────────────╯
```

## License

MIT
