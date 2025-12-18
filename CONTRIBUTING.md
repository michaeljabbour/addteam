# Contributing to addteam

Thanks for your interest in contributing!

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

# Or install in editable mode
uv pip install -e .
```

## Making Changes

1. Fork the repo
2. Create a branch (`git checkout -b my-feature`)
3. Make your changes
4. Test locally with `uv run addteam -n`
5. Commit (`git commit -m "Add feature"`)
6. Push and open a PR

## Code Style

- Python 3.10+
- Type hints throughout
- Keep it simple - this is a single-file tool

## Releasing

Releases are published to PyPI. To release:

```bash
# Bump version in pyproject.toml and bootstrap_repo.py
uv build
uvx twine upload dist/*
git tag vX.Y.Z && git push --tags
```

## Questions?

Open an issue or reach out to [@michaeljabbour](https://github.com/michaeljabbour).
