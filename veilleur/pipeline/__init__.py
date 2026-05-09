"""Scrape pipeline.

Wires the scraper, xpath toolkit, and validation logic together behind a
single ``run_scrape(feed_id)`` entry point. Globally serialized: at most
one scrape executes at a time process-wide, to keep load on a single
shared passe-partout instance bounded.
"""

from veilleur.pipeline.scrape import ScrapeOutcome, run_scrape

__all__ = ["ScrapeOutcome", "run_scrape"]
