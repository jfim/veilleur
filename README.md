# Veilleur

Monitors webpages and turns them into RSS/Atom feeds

Main features:
- Web UI to add webpages to turn into RSS feeds, alongside with polling frequency
- RSS feeds are served by veilleur, eg. /feeds/{id}/rss or /feeds/{id}/atom
- REST API to programmatically add new webpages to turn into RSS feeds and manage them
- REST API to fetch historical items that aren't present in the latest scrape, eg. /feeds/{id}/items
- Saves all of extracted items into a postgres database
- RSS feeds are extracted through XPath expressions (see in directory ~/projects/rss-ify/ the files dump_anchors.py, derive_xpath.py, prompt.txt)
- XPath expressions are built automatically using a LLM, as shown above
- When scanning, the list of new links should be compared with the ones in the previous batch:
  - If nothing matches, the page might have changed in ways that are incompatible, try regenerating a new xpath expression, and if that doesn't work, mark it as failed
  - New links should be evaluated to see if they match the longest common prefix in the previous run, excluding path parts that have numbers
    eg. if the previous run had example.com/posts/2026/mypost.html and example.com/posts/2026/another.html, the prefix used for comparison is example.com/posts/ (excluding the numeric part)
	This ensures that if the xpath expression matches new URL schemes like say example.com/something.html we can detect the xpath failure to work correctly
	If the new links don't match, regenerate an xpath expression, success if all links match previous ones or new links also match the correct prefix, fail if we get no matches or links that don't match the previous prefix
- Veilleur uses passe-partout's stateful tab flow (https://github.com/jfim/passe-partout) for fetching webpages and uses a wait for networkidle as part of the page flow. Only one page is fetched at a time (no parallelism/concurrency) and it does not use requests or other libraries to fetch webpages, it only uses HTTP libraries to talk to passe-partout
- HTML parsing should be robust against malformed HTML, pick a library that makes sense for this, maybe lxml.html.parse?

Dev setup:
- Use uv for package management (eg `uv init`)
- Use just for launching the various commands in the project
- Use ruff and pyright
- Set up github CI with proper caching
- Set up CLAUDE.md as necessary

## Docker

Build the image:

```sh
docker build -t veilleur .
```

The container exposes the FastAPI app on port `8000`. Run the database
migrations once before (or on each deploy of) a fresh DB:

```sh
docker run --rm --env-file .env veilleur alembic upgrade head
```

Then start the server:

```sh
docker run --rm -p 8000:8000 --env-file .env veilleur
```

### Configuration

All configuration is supplied via environment variables. The container reads
them directly — no `.env` file inside the image.

#### Postgres

| Variable | Default | Description |
| --- | --- | --- |
| `POSTGRES_HOST` | `localhost` | Postgres hostname. |
| `POSTGRES_PORT` | `5432` | Postgres port. |
| `POSTGRES_USER` | `veilleur` | Postgres user. |
| `POSTGRES_PASSWORD` | `veilleur` | Postgres password. |
| `POSTGRES_DB` | `veilleur` | Postgres database name. |

#### LLM (xpath derivation)

| Variable | Default | Description |
| --- | --- | --- |
| `LLM_API_URL` | _unset_ | Base URL for the OpenAI-compatible chat completions endpoint. |
| `LLM_MODEL_NAME` | _unset_ | Model identifier passed to the LLM endpoint. |
| `LLM_API_KEY` | _unset_ | Bearer token for the LLM endpoint. |
| `LLM_HTTP_TIMEOUT_SECONDS` | `300` | HTTP timeout for LLM calls (thinking models can take minutes). |

#### passe-partout (page fetching)

| Variable | Default | Description |
| --- | --- | --- |
| `PASSEPARTOUT_URL` | _unset_ | Base URL of the passe-partout service. |
| `PASSEPARTOUT_BEARER_TOKEN` | _unset_ | Bearer token for passe-partout. |

#### Scraping

| Variable | Default | Description |
| --- | --- | --- |
| `SCRAPE_DEFAULT_INTERVAL_SECONDS` | `3600` | Default poll interval for new feeds. |
| `SCRAPE_HTTP_TIMEOUT_SECONDS` | `60` | HTTP timeout when calling passe-partout. |
| `SCHEDULER_ENABLED` | `true` | Run the in-process scrape scheduler. Disable for worker-less deploys. |
| `SCHEDULER_TICK_SECONDS` | `30` | How often the scheduler wakes up to look for due feeds. |
| `VEILLEUR_RAW_HTML_DIR` | _unset_ | If set, an absolute path inside the container where raw fetched HTML is gzipped and persisted. Mount a volume here to keep it. |

#### XPath derivation

| Variable | Default | Description |
| --- | --- | --- |
| `VEILLEUR_XPATH_MAX_ANCHORS` | `250` | Maximum anchors sampled from a page when prompting the LLM. |
| `VEILLEUR_XPATH_MAX_ATTEMPTS` | `3` | Maximum number of attempts the LLM will make to derive a valid xpath expression before the feed is marked as failed. |
| `VEILLEUR_PROMPT_FILE` | _unset_ | Absolute path to a prompt-template override file. When unset the bundled default is used. |

#### API auth & misc

| Variable | Default | Description |
| --- | --- | --- |
| `VEILLEUR_API_BEARER_TOKEN` | _unset_ | Bearer token required by the programmatic REST API. When unset every API request except `/healthz` is rejected. |
| `LOG_LEVEL` | `INFO` | Python logging level. |
