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

# Run live (network) tests (requires LLM_API_KEY, LLM_API_URL, LLM_MODEL_NAME)
test-live:
    uv run pytest -m live

# Run dev server
run:
    uv run uvicorn veilleur.app:app --reload

# Lint + typecheck + test
check: lint typecheck test

# Bring up dev postgres
db-up:
    docker compose up -d postgres

# Tear down dev postgres (keeps volume)
db-down:
    docker compose down

# Generate a new migration: just db-migrate "add foo column"
db-migrate msg:
    uv run alembic revision --autogenerate -m "{{msg}}"

# Apply all migrations
db-upgrade:
    uv run alembic upgrade head

# Roll back one migration
db-downgrade:
    uv run alembic downgrade -1

# Drop the dev DB volume and re-run upgrade head
db-reset:
    docker compose down -v
    docker compose up -d postgres
    sleep 2
    uv run alembic upgrade head

# Verify metadata matches migrations (used in CI)
db-check:
    uv run alembic check
