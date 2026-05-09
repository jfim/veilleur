default:
    @just --list

# Install/sync dependencies
install:
    uv sync

# Lint with ruff
lint:
    uv run ruff check .

# Format code with ruff
format:
    uv run ruff format .

# Type-check with pyright
typecheck:
    uv run pyright

# Run tests
test:
    uv run pytest

# Run dev server
run:
    uv run uvicorn veilleur.app:app --reload

# Lint + typecheck + test
check: lint typecheck test
