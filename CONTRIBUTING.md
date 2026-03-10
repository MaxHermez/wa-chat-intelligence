# Contributing

Thank you for your interest in contributing to `wa-chat-intelligence`.

## Development Setup

**Requirements:** Python 3.12+, [uv](https://github.com/astral-sh/uv) (recommended) or pip.

```bash
# Clone the repo
git clone https://github.com/MaxHermez/wa-chat-intelligence.git
cd wa-chat-intelligence

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# Install in editable mode with all dependencies
pip install -e .

# Or with uv (faster):
uv pip install -e . --no-build-isolation

# Copy and configure environment variables
cp .env.example .env
# Edit .env — at minimum set OPENAI_API_KEY and CHAT_TXT_FILE
```

Verify the install:
```bash
wain --help
```

## Running the Pipeline on Test Data

Place a WhatsApp `.txt` export (and optionally its media files) in the location
specified by `CHAT_TXT_FILE` / `CHAT_EXPORT_DIR` in your `.env`, then run:

```bash
# Full pipeline in one command
wain run

# Or step by step
wain parse
wain transcribe          # requires WHISPER_BACKEND configured
wain describe            # requires VISION_BACKEND configured
wain chunk
wain summarize
wain embed

# Query the result
wain query
```

See `README.md` for the full environment variable reference and pipeline details.

## Code Style

No linter or formatter is configured yet. Follow the existing style:

- **PEP 8** — standard Python formatting; 4-space indentation, ~100 char line limit
- **Type hints** where already present — keep new code consistent
- **Parameterized SQL** — never f-string interpolation for queries
- **`config.py` as single source of truth** — all paths and settings via `wain.config`; call accessors at runtime (not module-level imports) for workspace-aware values
- **`typer.echo(..., err=True)`** for progress output, plain `print()` for data output

## Submitting a Pull Request

1. Fork the repository and create a feature branch:
   ```bash
   git checkout -b fix/short-description
   ```
2. Make your changes. Commit with a descriptive message:
   ```bash
   git commit -m "fix: short description of what changed"
   ```
3. Push and open a PR against `main`.
4. Describe what the PR changes and why. If it fixes a bug, include reproduction steps.

## Tests

There is no test suite yet — the `tests/` directory is a placeholder. Contributions
that add tests for the parser, chunker, or query layer are very welcome.

## License

This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for the full text.
