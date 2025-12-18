# Examples

Sample configurations for `addteam`.

## Files

| File | Description |
|------|-------------|
| [team.yaml](team.yaml) | Full example with all features |
| [team-minimal.yaml](team-minimal.yaml) | Simplest possible config |

## Quick Start

```bash
# Option 1: Generate from template
cd your-repo
addteam -i --init-action

# Option 2: Copy an example
curl -o team.yaml https://raw.githubusercontent.com/michaeljabbour/addteam/main/examples/team.yaml
```

## Role → Permission Mapping

| Role | Permission | Description |
|------|------------|-------------|
| `admins` | admin | Full access including settings |
| `maintainers` | maintain | Manage without admin settings |
| `developers` | push | Read/write code |
| `contributors` | push | Same as developers |
| `reviewers` | pull | Read-only |
| `readers` | pull | Same as reviewers |
| `triagers` | triage | Manage issues without code access |

## Expiring Access

```yaml
contractors:
  - username: temp-dev
    permission: push
    expires: 2025-06-01  # YYYY-MM-DD format
```

When you run `addteam --sync`, expired users are automatically removed.

## GitHub Teams

For organizations with GitHub Teams:

```yaml
teams:
  - myorg/backend-team           # Uses default_permission
  - myorg/frontend-team: push    # Explicit permission
  - myorg/security-team: pull    # Read-only
```

Requires `admin:org` scope on your GitHub token.
