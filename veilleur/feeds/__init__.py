"""Feed serialization and serving.

Renders stored items as RSS or Atom feeds (via feedgen) and exposes the
public ``/feeds/{id}/rss`` and ``/feeds/{id}/atom`` endpoints. The
authenticated ``/feeds/{id}/items`` history endpoint lives under
:mod:`veilleur.api.routes.feeds` instead.
"""

from veilleur.feeds.render import MAX_ITEMS, render_atom, render_rss
from veilleur.feeds.routes import router as feeds_public_router

__all__ = ["MAX_ITEMS", "feeds_public_router", "render_atom", "render_rss"]
