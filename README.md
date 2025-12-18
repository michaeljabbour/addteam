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

Defaults:

- Uses `collaborators.txt` from the repo root (even if you run it from a subdirectory)
- Targets the repo for the current directory (unless `--repo` is set)
- Skips the repo owner and the currently authenticated user
- AI blurb: tries OpenAI first, then Anthropic (if keys are present)

## Run from anywhere (no clone)

```bash
uvx git+https://github.com/michaeljabbour/addteam@main --repo=michaeljabbour/addteam
```

When `--repo` is set, it reads `collaborators.txt` from that repo automatically.

## Fastest path (inside a repo)

From inside the repo you want to manage:

```bash
uvx git+https://github.com/michaeljabbour/addteam@main
```

## Collaborators file

```bash
# use a different file in the repo (path is inside the target repo)
uvx --from git+https://github.com/michaeljabbour/addteam@main addteam --repo=michaeljabbour/addteam --collaborators-file=collaborators-prod.txt

# force reading from the target repo
uvx --from git+https://github.com/michaeljabbour/addteam@main addteam --repo=michaeljabbour/addteam --collaborators-file=repo:collaborators-prod.txt

# force reading a local file (relative paths resolve from the repo root if available)
uvx --from git+https://github.com/michaeljabbour/addteam@main addteam --repo=michaeljabbour/addteam --collaborators-file=local:./collaborators.txt
```

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

# force a specific AI provider (no fallback)
uv run scripts/bootstrap_repo.py --provider=openai
uv run scripts/bootstrap_repo.py --provider=anthropic

# write the AI blurb into README.md (between markers)
uv run scripts/bootstrap_repo.py --write-readme
```

## Optional install (faster repeat runs)

```bash
uv tool install --force git+https://github.com/michaeljabbour/addteam@main
addteam --repo=michaeljabbour/addteam
```

## AI summary (optional)

If `OPENAI_API_KEY` and/or `ANTHROPIC_API_KEY` is set, the script prints a short repo blurb after inviting collaborators (tries OpenAI first, then Anthropic).
