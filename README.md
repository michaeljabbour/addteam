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
  - username: charlie
    name: Charlie Day      # optional display name

reviewers:
  - eve
```

`name:` is optional anywhere a `username:` is given — it shows up in
audit/apply output, `--json`, and addresses welcome issues by name.

Role names map to permissions automatically:

| Role | Permission |
|------|------------|
| `admins` | admin |
| `maintainers` | maintain |
| `developers`, `contributors`, `contractors` | push |
| `reviewers`, `readers` | pull |
| `triagers` | triage |

Unknown keys produce a warning (not silent drops), so a typo like
`develoeprs:` won't quietly shrink your team.

> Note: `maintain` and `triage` only exist on **organization-owned repos**.
> On personal repos addteam automatically degrades to push/pull with a
> warning (never expands access), so one config works everywhere.

## Common Commands

```bash
addteam                    # invite from local team.yaml
addteam -n                 # dry-run (preview only)
addteam -a                 # audit (show drift)
addteam -s                 # sync (also removes unlisted users, asks first)
addteam --from org/config  # use team.yaml from another repo
```

Apply mode also fixes permission drift: if a collaborator exists with the
wrong permission, it is updated to match team.yaml.

## Central Team Config

Keep one `team.yaml` in a shared repo, apply it anywhere:

```bash
# Anyone on your team can run this in any repo:
uvx addteam --from myorg/team-config
```

(`addteam myorg/team-config` without `--from` still works and means the same
thing; `--from` is the explicit, less ambiguous spelling. Confusing it with
`-r owner/repo` — which selects the *target* repo — is a common mistake.)

Apply only one group (e.g. just the leads) to a specific repo:

```bash
uvx addteam --from myorg/team-config --group maintainers -r myorg/the-repo
```

The AI summary at the end is perfect for sharing via email or Slack.

## Options

| Flag | Description |
|------|-------------|
| `-n, --dry-run` | Preview without making changes |
| `-s, --sync` | Remove collaborators not in list |
| `-a, --audit` | Show drift without making changes |
| `--fail-on-drift` | With `--audit`: exit 1 when drift is found (CI gates) |
| `-r, --repo` | Target a specific repo |
| `--from` | Fetch team.yaml from another repo |
| `--group ROLE` | Only apply these role groups (repeatable; never with `--sync`) |
| `--json` | Machine-readable output for audit/apply |
| `-y, --yes` | Skip confirmation prompts (sync removals) |
| `-q, --quiet` | Minimal output |
| `--welcome` / `--no-welcome` | Turn welcome issues on/off for this run (default: off) |
| `--no-ai` | Skip AI-generated summaries |
| `--report DIR` | Permission matrix for every repo in a directory |
| `--csv PATH` | With `--report`: also write a spreadsheet |
| `--format long/matrix` | With `--report --csv`: row-per-user or grid layout |
| `--no-names` | With `--report`: skip display-name lookups (faster) |

Exit codes: `0` success · `1` runtime error (or drift with `--fail-on-drift`) · `2` usage error.

### Directory Report

Audit every repo in a folder at once:

```bash
addteam --report ~/dev
addteam --report ~/dev --csv access.csv --format matrix
```

Scans each subdirectory with a git working copy, collects collaborators
(active and pending invitations) with permissions, looks up display names,
shows a users × repos matrix in the terminal, and optionally writes a CSV
spreadsheet. Also available as `--json` for scripting.

### Scripting

```bash
addteam --audit --json | jq '.drift'
addteam --audit --fail-on-drift   # CI gate: fail the job on drift
```

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
    expires: 2027-06-01
```

Expired users are skipped on apply and removed on `--sync`.

### GitHub Teams (orgs)

```yaml
teams:
  - myorg/backend-team
  - myorg/frontend-team: pull
```

If a team lookup fails, the affected members are skipped with a warning and
`--sync` refuses to run — a partial config must never cause mass removals.

### Multi-Repo Management

```bash
addteam --init-multi-repo
```

Creates `repos.txt` to sync the same team across multiple repos.

### Welcome Issues

addteam can create a welcome issue for each new collaborator, with an
AI-generated summary of your repo (requires `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `OPENROUTER_API_KEY`).

Welcome issues are off by default. Turn them on per run with `--welcome`,
or per repo in config:

```yaml
welcome_issue: true
```

`--no-welcome` always forces them off, even if config sets `welcome_issue: true`.

## License

MIT
