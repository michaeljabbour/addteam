# addteam

One-command collaborator management for GitHub repos with GitOps support.

## Quick Start

```bash
# From your repo directory
uvx addteam                         # use local team.yaml
uvx addteam owner/repo              # use team.yaml from another repo
uvx addteam -n                      # preview (dry-run)
```

**Prerequisites:** [GitHub CLI](https://cli.github.com/) installed and authenticated.

## What It Does

1. Reads team config from `team.yaml`
2. Invites collaborators with role-based permissions
3. Detects drift between config and GitHub
4. Optionally syncs (removes unlisted users)
5. Supports GitOps via GitHub Actions

## Setup

### First-Time Setup

```bash
# Create starter team.yaml
addteam -i

# Also create GitHub Action for GitOps
addteam -i --init-action
```

This creates:
- `team.yaml` - your team configuration (commit this!)
- `.github/workflows/sync-collaborators.yml` - auto-sync on push

### team.yaml Format

```yaml
# Team configuration
default_permission: push

# Role-based groups (permission inferred from role name)
admins:
  - alice

developers:
  - bob
  - charlie

reviewers:
  - eve

# Temporary access with expiry dates (optional)
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

> **Note:** `team.yaml` should be committed to your repo (not gitignored). It's your source of truth for access control.

## Usage

```bash
addteam [source] [options]
```

Where `source` is either:
- A local file (default: `team.yaml`)
- A GitHub repo (`owner/repo`) to fetch team.yaml from

### Common Options

| Short | Long | Description |
|-------|------|-------------|
| `-n` | `--dry-run` | Preview without making changes |
| `-s` | `--sync` | Remove collaborators not in list |
| `-a` | `--audit` | Show drift without making changes |
| `-w` | `--welcome` | Create welcome issues for new users |
| `-q` | `--quiet` | Minimal output |
| `-r` | `--repo OWNER/REPO` | Target a different repo |

### Init Options

| Short | Long | Description |
|-------|------|-------------|
| `-i` | `--init` | Create starter `team.yaml` |
| | `--init-action` | Create GitHub Action workflow |
| | `--init-multi-repo` | Create multi-repo sync workflow |

### Examples

```bash
# Setup a new repo
addteam -i --init-action

# Apply team from another repo
addteam myorg/team-config
addteam myorg/team-config -n    # preview first

# Preview what would happen
addteam -n

# Audit mode: see who has access vs who should
addteam -a

# Sync mode: add missing, remove unlisted (preview first!)
addteam -s -n
addteam -s

# Invite with welcome issues
addteam -w
```

## Using a Central Team Config

Maintain a central `team.yaml` in one repo and apply it anywhere:

```bash
cd your-repo
addteam myorg/team-config         # apply team from central repo
addteam myorg/team-config -n      # preview first
```

This is useful when:
- You have a standard team across multiple repos
- You want a "reference" repo with your org's team members
- You're bootstrapping new repos with existing team access

## GitOps Workflow

The recommended approach is to use `team.yaml` as your source of truth with automatic enforcement via GitHub Actions.

### Why GitOps?

- **Auditable**: All changes go through PRs
- **Reviewable**: Team leads approve access changes
- **Consistent**: No drift between config and reality
- **Reversible**: Git history shows when/why/who

### Setup

```bash
# In your repo
addteam -i --init-action
git add team.yaml .github/
git commit -m "Add team access management"
git push
```

### GitHub Token Setup

The Action needs a PAT (Personal Access Token) with appropriate scopes:

1. Go to GitHub → Settings → Developer settings → Personal access tokens
2. Create a token with:
   - `repo` scope (for collaborator management)
   - `admin:org` scope (if using GitHub Teams)
3. Add as repository secret: `TEAM_SYNC_TOKEN`

### Workflow

```
Developer submits PR to add Alice
    ↓
Team lead reviews and approves
    ↓
PR merged to main
    ↓
GitHub Action runs `addteam --sync`
    ↓
Alice gets access (and welcome issue)
```

## Multi-Repo Management

For organizations managing access across many repos:

```bash
# Create multi-repo workflow
addteam --init-multi-repo
```

This creates:
- `team.yaml` - shared team configuration
- `repos.txt` - list of repos to manage
- `.github/workflows/sync-team.yml` - syncs all repos

Edit `repos.txt`:
```
# Repos to sync (one per line)
myorg/frontend
myorg/backend
myorg/docs
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

  total drift: 3 item(s)
```

## Welcome Issues

When `--welcome` is enabled, new collaborators automatically get a welcome issue with:

- AI-generated repo summary (if API key set)
- Getting started checklist
- Assignment to the new collaborator

Enable in YAML:
```yaml
welcome_issue: true
```

Or via CLI:
```bash
addteam -w
```

## Expiring Access

For temporary collaborators (contractors, interns):

```yaml
contractors:
  - username: temp-dev
    permission: push
    expires: 2025-06-01
```

- Expired users are skipped during invite
- With `--sync`, expired users are automatically removed
- Audit mode shows expired access

> **Note:** Expiry is enforced client-side when you run `addteam`. For automatic enforcement, use the GitOps workflow with scheduled runs.

## Installation

### One-liner (no install)

```bash
uvx addteam --help
```

### Install globally

```bash
pip install addteam
addteam --help
```

### From source

```bash
git clone https://github.com/michaeljabbour/addteam
cd addteam
pip install -e .
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | For AI-generated welcome messages |
| `ANTHROPIC_API_KEY` | Alternative AI provider |
| `GH_TOKEN` | GitHub token (used by `gh` CLI) |

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Your Repo                                              │
│                                                         │
│  team.yaml ──────────────────┐                         │
│  (source of truth)           │                         │
│                              ▼                         │
│  .github/workflows/    ┌──────────┐                    │
│  sync-collaborators.yml│ addteam  │──► GitHub API      │
│  (on push to main)     └──────────┘                    │
│                              │                         │
│                              ▼                         │
│                        Collaborators                   │
│                        synchronized                    │
└─────────────────────────────────────────────────────────┘
```

## FAQ

**Should team.yaml be in .gitignore?**

No! It should be committed. It's your source of truth for access control, and you want the Git history.

**What if I want different configs for different repos?**

Each repo can have its own `team.yaml`. Or use multi-repo mode with a central config.

**Does this work with GitHub Enterprise?**

Yes, as long as `gh` CLI is configured for your enterprise instance.

**What permissions does the GitHub token need?**

- `repo` scope for collaborator management
- `admin:org` if using GitHub Teams integration

## License

MIT
