# addteam

One-command access + onboarding bootstrap for this repo.

## Prereqs

- GitHub CLI (`gh`) installed and authenticated
- `uv` installed
- You must have admin access on the repo to add collaborators

## Add collaborators (one line)

```bash
uv run scripts/bootstrap_repo.py
```

It reads `collaborators.txt`, skips the repo owner (and the authenticated user), and invites everyone else.

## Useful options

```bash
# see what it would do
uv run scripts/bootstrap_repo.py --dry-run

# add just one user (even if not in collaborators.txt)
uv run scripts/bootstrap_repo.py --user octocat

# target a specific repo (instead of inferring from the current directory)
uv run scripts/bootstrap_repo.py --repo michaeljabbour/addteam

# remove direct collaborators not in collaborators.txt (offboarding)
uv run scripts/bootstrap_repo.py --sync --dry-run

# skip the AI blurb
uv run scripts/bootstrap_repo.py --no-ai

# write the AI blurb into README.md (between markers)
uv run scripts/bootstrap_repo.py --write-readme
```

## AI summary (optional)

If `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` is set, the script prints a short repo blurb after inviting collaborators.
