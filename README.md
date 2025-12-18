# addteam

Invite GitHub collaborators from a simple YAML file.

## Install

```bash
pip install addteam
```

Or run without installing:

```bash
uvx addteam
```

**Prerequisite:** [GitHub CLI](https://cli.github.com/) must be installed and authenticated (`gh auth login`).

## First Run

```bash
# In your repo directory, create a team.yaml
addteam --init

# Edit team.yaml with your team members, then run:
addteam
```

That's it! Your collaborators will be invited.

---

## team.yaml Format

```yaml
default_permission: push

admins:
  - alice

developers:
  - bob
  - charlie

reviewers:
  - eve
```

Role names map to permissions automatically:

| Role | Permission |
|------|------------|
| `admins` | admin |
| `maintainers` | maintain |
| `developers`, `contributors` | push |
| `reviewers`, `readers` | pull |
| `triagers` | triage |

## Common Commands

```bash
addteam                    # invite from local team.yaml
addteam -n                 # dry-run (preview only)
addteam -a                 # audit (show drift)
addteam -s                 # sync (also removes unlisted users)
addteam owner/repo         # use team.yaml from another repo
```

## Central Team Config

Keep one `team.yaml` in a shared repo, apply it anywhere:

```bash
# Anyone on your team can run this in any repo:
uvx addteam myorg/team-config
```

The AI summary at the end is perfect for sharing via email or Slack.

## Options

| Flag | Description |
|------|-------------|
| `-n, --dry-run` | Preview without making changes |
| `-s, --sync` | Remove collaborators not in list |
| `-a, --audit` | Show drift without making changes |
| `-r, --repo` | Target a specific repo |
| `-q, --quiet` | Minimal output |
| `--no-welcome` | Skip creating welcome issues |
| `--no-ai` | Skip AI-generated summaries |

## GitOps Setup

Automatically sync collaborators when team.yaml changes:

```bash
addteam --init --init-action
```

This creates:
- `team.yaml` - your team config
- `.github/workflows/sync-collaborators.yml` - runs on push

Add a `TEAM_SYNC_TOKEN` secret (PAT with `repo` scope) to your repo.

## Advanced Features

### Expiring Access

```yaml
contractors:
  - username: temp-dev
    permission: push
    expires: 2025-06-01
```

### GitHub Teams (orgs)

```yaml
teams:
  - myorg/backend-team
  - myorg/frontend-team: pull
```

### Multi-Repo Management

```bash
addteam --init-multi-repo
```

Creates `repos.txt` to sync the same team across multiple repos.

### Welcome Issues

New collaborators automatically get a welcome issue with an AI-generated summary of your repo (requires `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GOOGLE_API_KEY`).

## License

MIT
