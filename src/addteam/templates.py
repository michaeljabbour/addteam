"""File templates created by `addteam --init` variants."""

from __future__ import annotations

TEAM_YAML_TEMPLATE = """\
# Team configuration for {repo_name}
# Docs: https://github.com/michaeljabbour/addteam

default_permission: push

# Role-based groups (permission inferred from role name)
admins:
  - {owner}

developers:
  # - alice
  # - bob

# reviewers:
#   - eve

# Temporary access with expiry dates
# contractors:
#   - username: temp-dev
#     permission: push
#     expires: 2027-06-01

# GitHub team integration (for orgs)
# teams:
#   - myorg/backend-team
#   - myorg/frontend-team: pull

# Auto-create welcome issues for new collaborators (off by default)
# welcome_issue: true
"""

GITHUB_ACTION_TEMPLATE = """\
# Sync collaborators on push to team.yaml
# This workflow enforces team.yaml as the source of truth for repo access

name: Sync Collaborators

on:
  push:
    branches: [main]
    paths:
      - 'team.yaml'
  workflow_dispatch:  # Allow manual trigger

jobs:
  sync:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      issues: write  # For welcome issues (optional)

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install addteam
        run: pip install addteam

      - name: Sync collaborators
        env:
          GH_TOKEN: ${{{{ secrets.TEAM_SYNC_TOKEN }}}}
          # Optional: for AI-generated welcome messages
          # OPENAI_API_KEY: ${{{{ secrets.OPENAI_API_KEY }}}}
        run: |
          addteam --sync --no-ai --yes
"""

GITHUB_ACTION_MULTI_REPO_TEMPLATE = """\
# Sync collaborators across multiple repos
# This workflow uses this repo as the source of truth for team membership

name: Sync Team Across Repos

on:
  push:
    branches: [main]
    paths:
      - 'team.yaml'
      - 'repos.txt'
  workflow_dispatch:
  schedule:
    - cron: '0 9 * * 1'  # Weekly on Monday 9am UTC

jobs:
  sync:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install addteam
        run: pip install addteam

      - name: Sync all repos
        env:
          GH_TOKEN: ${{{{ secrets.TEAM_SYNC_TOKEN }}}}
        run: |
          # Read repos from repos.txt (one per line)
          while IFS= read -r repo || [[ -n "$repo" ]]; do
            [[ "$repo" =~ ^#.*$ || -z "$repo" ]] && continue
            echo "::group::Syncing $repo"
            addteam -r "$repo" -f team.yaml --sync --no-ai --yes || echo "Failed: $repo"
            echo "::endgroup::"
          done < repos.txt
"""

REPOS_TXT_TEMPLATE = """\
# Repos to sync with team.yaml (one per line)
# Lines starting with # are ignored

# Example:
# myorg/repo1
# myorg/repo2
# myorg/repo3
"""
