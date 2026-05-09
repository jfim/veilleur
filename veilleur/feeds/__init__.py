"""Feed serialization and serving.

Renders stored items as RSS or Atom feeds (via feedgen) and exposes the
``/feeds/{id}/rss``, ``/feeds/{id}/atom``, and ``/feeds/{id}/items``
endpoints.
"""
