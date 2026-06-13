"""Scrape pipeline.

Wires the scraper, xpath toolkit, and validation logic together behind a
single ``run_scrape(feed_id)`` entry point. Globally serialized: at most
one scrape executes at a time process-wide, to keep load on a single
shared passe-partout instance bounded.
"""

from veilleur.pipeline.scrape import (
    STALE_RUN_TIMEOUT,
    ScrapeOutcome,
    recover_stale_runs,
    run_scrape,
)

__all__ = ["STALE_RUN_TIMEOUT", "ScrapeOutcome", "recover_stale_runs", "run_scrape"]
