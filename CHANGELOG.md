# Changelog

All notable changes to this project will be documented in this file.

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
