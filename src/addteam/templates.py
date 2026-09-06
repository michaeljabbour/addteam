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
# Sync collaborators on push to team.yaml, and preview the plan on PRs.
# This workflow enforces team.yaml as the source of truth for repo access.

name: Sync Collaborators

on:
  push:
    branches: [main]
    paths:
      - 'team.yaml'
  pull_request:
    paths:
      - 'team.yaml'
  workflow_dispatch:  # Allow manual trigger

jobs:
  sync:
    if: github.event_name != 'pull_request'
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
          GH_TOKEN: ${{ secrets.TEAM_SYNC_TOKEN }}
          # Optional: for AI-generated welcome messages
          # OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        run: |
          # Deliberate large roster cuts: raise --max-removals or add --allow-mass-removal (see README "Sync Safety")
          addteam --sync --no-ai --yes --max-removals 3 --json-out addteam-run.json

      - name: Post run summary
        if: always()
        run: |
          if [ -f addteam-run.json ]; then
            {
              echo "### addteam sync summary"
              echo ""
              jq -r '.summary | to_entries | map("- **\\(.key)**: \\(.value)") | join("\\n")' addteam-run.json
            } >> "$GITHUB_STEP_SUMMARY"
          fi

      - name: Upload run artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: addteam-run
          path: addteam-run.json
          if-no-files-found: ignore

  plan:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install addteam
        run: pip install addteam

      - name: Preview sync plan
        continue-on-error: true
        env:
          GH_TOKEN: ${{ secrets.TEAM_SYNC_TOKEN }}
        run: |
          addteam --sync --dry-run --no-ai --json-out plan.json

      - name: Post plan comment
        if: always()
        env:
          GH_TOKEN: ${{ secrets.TEAM_SYNC_TOKEN }}
        run: |
          {
            echo "### addteam sync plan"
            echo ""
            if [ -f plan.json ]; then
              echo '```json'
              cat plan.json
              echo '```'
            else
              echo "_Preview failed to generate a plan — check the workflow run for details._"
            fi
          } > plan-comment.md
          gh pr comment "${{ github.event.pull_request.number }}" --repo "${{ github.repository }}" --body-file plan-comment.md --edit-last || \
            gh pr comment "${{ github.event.pull_request.number }}" --repo "${{ github.repository }}" --body-file plan-comment.md
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
          GH_TOKEN: ${{ secrets.TEAM_SYNC_TOKEN }}
        run: |
          failed=0
          # Read repos from repos.txt (one per line)
          while IFS= read -r repo || [[ -n "$repo" ]]; do
            [[ "$repo" =~ ^#.*$ || -z "$repo" ]] && continue
            echo "::group::Syncing $repo"
            out="addteam-run-${repo//\\//-}.json"
            # Deliberate large roster cuts: raise --max-removals or add --allow-mass-removal (see README "Sync Safety")
            addteam -r "$repo" -f team.yaml --sync --no-ai --yes --max-removals 3 --json-out "$out" || { echo "::error::addteam failed for $repo"; failed=1; }
            echo "::endgroup::"
          done < repos.txt
          if [ "$failed" = "1" ]; then exit 1; fi

      - name: Upload run artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: addteam-runs
          path: addteam-run-*.json
          if-no-files-found: ignore
"""

REPOS_TXT_TEMPLATE = """\
# Repos to sync with team.yaml (one per line)
# Lines starting with # are ignored

# Example:
# myorg/repo1
# myorg/repo2
# myorg/repo3
"""
