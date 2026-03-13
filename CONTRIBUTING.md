# Contributing

Thanks for your interest in contributing to `wain`!

## Development setup

**Requirements:** Python 3.12+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/MaxHermez/wa-chat-intelligence.git
cd wa-chat-intelligence
uv venv && source .venv/bin/activate
uv pip install setuptools wheel
uv pip install -e ".[dev]" --no-build-isolation
```

Verify the install:
```bash
wain --help
```

## Branching

- **`main`** is the stable release branch. Do not push directly to `main`.
- **`dev`** is the integration branch. All work merges here first.
- Create feature branches off `dev`, named after the issue: `hhc-115-topics-index`.
- When `dev` is stable and ready for release, it gets merged to `main` via PR.

```
main  <-- release merges only
 |
dev   <-- feature branches merge here
 |
hhc-115-topics-index  <-- your work
```

## Making changes

1. Branch off `dev`:
   ```bash
   git checkout dev && git pull origin dev
   git checkout -b hhc-XXX-short-description
   ```

2. Make your changes. Follow existing code style -- no linter config yet, just match what's there.

3. Run tests:
   ```bash
   uv run python -m pytest tests/ -v
   ```

4. Push and open a PR targeting `dev`:
   ```bash
   git push -u origin hhc-XXX-short-description
   ```

## Code conventions

- **No personal names** in code, docs, or examples. Use Alice/Bob as generic names.
- **ASCII-only in print() output.** No Unicode symbols (checkmarks, em-dashes, etc.) -- Windows cp1252 encoding breaks on them.
- **Parameterized SQL** -- never f-string interpolation for queries.
- **`config.py` as single source of truth** -- all paths and settings via `wain.config`; call accessors at runtime (not module-level imports) for workspace-aware values.
- Keep changes focused. One issue per PR.

## Issue tracking

The project uses [Linear](https://linear.app) for issue tracking (team: HHC, project: wa-chat-intelligence). If you don't have Linear access, open a GitHub Issue instead -- we'll transfer it.

Issue template:

```
## Context
Why this needs to exist / what problem it solves.

## Task
- Specific step 1
- Specific step 2

## Acceptance criteria
- [ ] Thing that must be true when done
- [ ] Another thing

## Notes
Anything relevant -- gotchas, related files, dependencies.
```

## Tests

Tests run offline (no API key needed). They cover the parser, chunker, config, query filtering, and CLI.

```bash
uv run python -m pytest tests/ -v
```

If your change touches search or embedding logic, also do a manual sanity check against real data if you have a populated workspace.

## Pull request guidelines

- Fill out the PR template (summary, related issue, test plan).
- PRs target `dev`, not `main`.
- Squash merge is the default -- keep your PR title clean, it becomes the commit message.
- PRs require at least 1 approval before merge.

## Releases

Releases are cut from `main` and published to PyPI. Version is bumped in both `pyproject.toml` and `wain/__init__.py`. Only maintainers handle releases.

## License

This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for the full text.
