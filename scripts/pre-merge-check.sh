#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv sync --locked
uv run --locked ruff check .
uv run --locked ruff format --check src tests
uv run --locked mypy src
uv run --locked python -m unittest discover -s tests -v
uv run --locked potd-trader --help >/dev/null
uv run --locked potd-trader --version
