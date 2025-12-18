# Contributing to addteam

Thanks for your interest in contributing! This is a small, focused tool and we want to keep it that way.

## Philosophy

- **Simple**: Single Python file, minimal dependencies
- **Practical**: Solve real problems for real teams
- **Reliable**: Works the same every time

## Quick Start

```bash
git clone https://github.com/michaeljabbour/addteam
cd addteam
uv sync
uv run addteam --help
```

## Development

```bash
# Run from source
uv run python -m addteam --help

# Run tests
uv run pytest

# Type check (optional)
uv run mypy src/addteam
```

## Making Changes

1. **Open an issue first** for significant changes
2. Fork the repo
3. Create a branch (`git checkout -b my-feature`)
4. Make your changes
5. Test locally with `uv run addteam -n` (dry-run)
6. Commit with a clear message
7. Push and open a PR

## What We're Looking For

**Good contributions:**
- Bug fixes with reproduction steps
- Documentation improvements
- New AI providers (with API key detection)
- Better error messages

**Probably not:**
- Major architectural changes
- Features that require config files
- Dependencies that aren't strictly necessary

## Code Style

- Python 3.10+
- Type hints throughout
- Keep it simple - this is intentionally a single-file tool
- Run `ruff check src/` before submitting

## Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=addteam
```

When adding features, add tests in `tests/`.

## Releases

Maintainers handle releases. The process:

```bash
# 1. Bump version in pyproject.toml AND bootstrap_repo.py
# 2. Update CHANGELOG.md
# 3. Commit and push
git commit -m "vX.Y.Z: Description"
git push

# 4. Build and publish
uv build
uvx twine upload dist/*

# 5. Tag
git tag vX.Y.Z && git push --tags
```

## Questions?

- **Bug?** Open an issue with reproduction steps
- **Feature idea?** Open an issue to discuss first
- **Question?** Start a discussion or reach out to [@michaeljabbour](https://github.com/michaeljabbour)

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
