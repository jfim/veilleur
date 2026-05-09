"""ORM models, imported here so Alembic autogenerate sees the full metadata."""

from veilleur.db.models.feed import Feed
from veilleur.db.models.feed_item import FeedItem
from veilleur.db.models.scrape_run import ScrapeRun
from veilleur.db.models.xpath_extractor import XPathExtractor

__all__ = ["Feed", "FeedItem", "ScrapeRun", "XPathExtractor"]
