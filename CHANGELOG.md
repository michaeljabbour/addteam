# Changelog

All notable changes to this project will be documented in this file.

## [0.8.5] - 2024-12-18

### Changed
- Improved AI summary prompt with structured output format
- Summary now includes: repo name, URL, what it does, install, quick start
- Prefers uvx/pipx over pip install
- No emojis, no markdown, no fluff

## [0.8.4] - 2024-12-18

### Changed
- AI summary now always displays (useful for sharing via email/Slack)
- Shows "Repo summary (for sharing)" when no new invites sent

## [0.8.3] - 2024-12-18

### Changed
- Friendlier first-run experience when no team.yaml found
- Shows helpful guidance instead of error message
- Exit code 0 (not an error, just needs setup)

## [0.8.2] - 2024-12-18

### Changed
- AI summary now displayed at END of run (after invites complete)
- AI prompt requests plain text output (no markdown formatting)
- Cleaner terminal output for welcome messages

## [0.8.1] - 2024-12-18

### Changed
- Clearer status messages for collaborator states:
  - "already has access" - user accepted invitation
  - "already invited" - invitation pending acceptance
  - "invited" - newly invited

## [0.8.0] - 2024-12-18

### Added
- Skip existing collaborators - no duplicate invites or welcome issues
- Display AI welcome summary in terminal before sending

### Changed
### Changed
- Users already on repo show "already push" instead of being re-invited


### Added
- AI provider status feedback (shows which provider is used or why none available)

## [0.7.1] - 2024-12-18

### Fixed
- Update notification now shows uvx refresh hint

## [0.7.0] - 2024-12-18

### Changed
- **Welcome issues ON by default**: AI-powered welcome issues are now created automatically
  - Use `--no-welcome` to disable
  - Use `--no-ai` to skip AI summary but still create basic welcome issues

### Added
- **Google Gemini support**: Added `GOOGLE_API_KEY` as third fallback for AI summaries
- **OpenRouter support**: Added `OPENROUTER_API_KEY` as fourth fallback
- AI provider priority: OpenAI → Anthropic → Google → OpenRouter
- `--provider` flag now accepts: `auto`, `openai`, `anthropic`, `google`, `openrouter`

## [0.6.0] - 2024-12-18

### Added
- **Auto-update check**: Notifies users when a newer version is available on PyPI
- Check runs on each invocation (2s timeout, fails silently)
- Shows: `update available: 0.6.0 → 0.7.0  (pip install -U addteam)`

## [0.5.1] - 2024-12-18

### Improved
- **Enhanced welcome issues**: Now include repo metadata, language-specific setup hints, and contextual onboarding
- **Smarter AI summaries**: AI now reads README content to generate accurate install/usage instructions
- Increased AI max_tokens from 200 to 500 for richer welcome messages

### Added
- `_get_repo_info()`: Fetches repo description, topics, language, and homepage
- `_get_readme_excerpt()`: Fetches README content for AI context
- Language-specific setup hints (Python, JavaScript, TypeScript, Rust, Go)
- Topics display in welcome issues
- Direct links to README and homepage

## [0.5.0] - 2024-12-18

### Changed
- **Simplified CLI**: Config source is now a positional argument
  - Before: `addteam -f owner/repo`
  - After: `addteam owner/repo`
- `-f` flag kept for backwards compatibility

### Added
- `py.typed` marker for type checking support
- `CONTRIBUTING.md` guide

## [0.4.0] - 2024-12-18

### Changed
- **Breaking**: Renamed internal package from `addmadeteam` to `addteam`
- Adopted `src/` layout (modern Python packaging best practice)
- Clean git history (no traces of original internal name)

### Added
- Remote config fetch: `-f owner/repo` fetches team.yaml from another GitHub repo
- Examples directory with sample configurations

## [0.3.1] - 2024-12-18

### Added
- Remote repo support for `-f` flag
- PyPI package publication
- Comprehensive README with GitOps documentation

### Fixed
- License format for modern setuptools compatibility

## [0.3.0] - 2024-12-18

### Added
- GitOps workflow with `--init-action`
- Multi-repo management with `--init-multi-repo`
- Audit mode (`-a`) for drift detection
- Welcome issues with AI-generated summaries
- Expiring access support

## [0.2.0] - 2024-12-18

### Added
- Role-based permission inference (admins → admin, developers → push, etc.)
- GitHub Teams integration
- YAML configuration format

## [0.1.0] - 2024-12-18

### Added
- Initial release
- Basic collaborator management
- Dry-run mode
- Sync mode (add/remove)
