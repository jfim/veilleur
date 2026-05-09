# Veilleur

Monitors webpages and turns them into RSS/Atom feeds. Items are extracted via
LLM-derived XPath expressions, persisted in Postgres, and served as feeds and
through a REST API. See `README.md` for the full spec.

## Commands

Use `just` for all common tasks:

- `just install` — sync dependencies via uv
- `just lint` — ruff check
- `just format` — ruff format
- `just typecheck` — pyright (strict on `veilleur/`)
- `just test` — pytest
- `just run` — uvicorn dev server (`veilleur.app:app`, --reload)
- `just check` — lint + typecheck + test (the CI gate)

## Layout

- `veilleur/app.py` — FastAPI entry point; mounts UI, REST API, and feed routes.
- `veilleur/config.py` — pydantic-settings `Settings` (env prefix `VEILLEUR_`).
- `veilleur/db/` — SQLAlchemy models, session factory, Alembic migrations.
  Stores feed definitions and the full history of scraped items.
- `veilleur/scraper/` — fetches pages via passe-partout, parses with lxml,
  extracts items using the stored xpath. Strictly serial, no concurrency.
- `veilleur/xpath/` — LLM-driven xpath derivation plus the
  longest-common-prefix validation that detects drift between runs.
- `veilleur/feeds/` — RSS/Atom rendering via feedgen and the
  `/feeds/{id}/{rss,atom,items}` routes.
- `tests/` — pytest suite.

## External dependencies

- **passe-partout** (https://github.com/jfim/passe-partout) — the only way
  pages are fetched. Use its stateful tab flow with a `wait for networkidle`
  step. Talk to it over HTTP only; never call `requests`/`httpx` directly
  against the target site.
- **rss-ify** at `~/projects/rss-ify/` — reference implementation for the
  xpath derivation. See `dump_anchors.py`, `derive_xpath.py`, `prompt.txt`.

## Conventions

- **uv** owns dependencies — add with `uv add <pkg>` (or `uv add --dev`).
  Never edit a `requirements.txt`; there isn't one.
- **just** owns task entry points — extend `justfile` rather than scripting
  things ad-hoc.
- **ruff + pyright** are the bar — `just check` must pass before merging.
  Pyright runs in strict mode on the `veilleur/` package.
- Python 3.12+, line length 100.
