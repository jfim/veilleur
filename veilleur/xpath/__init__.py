"""XPath derivation and validation.

Uses an LLM to derive an xpath expression that selects feed items from a
page (mirroring the approach in ~/projects/rss-ify/). Also validates xpath
output across runs by comparing new links to the previous batch's longest
common prefix (ignoring numeric path parts) and triggers regeneration on
drift.
"""
