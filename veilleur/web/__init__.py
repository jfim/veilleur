"""Server-rendered web UI (Phase 9).

Minimal Jinja2-driven pages for managing feeds. Auth piggybacks on the
REST API bearer token via HTTP Basic — see :mod:`veilleur.web.auth`. The
router exposes operator-facing form endpoints under ``/ui`` plus a top-level
``/`` index.
"""

from veilleur.web.routes import (
    STATIC_DIR,
    STATIC_PATH,
    web_router,
)

__all__ = ["STATIC_DIR", "STATIC_PATH", "web_router"]
