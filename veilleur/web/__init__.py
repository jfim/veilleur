"""Server-rendered web UI (Phase 9).

Minimal Jinja2-driven pages for managing feeds. Auth uses a session
cookie containing the API bearer token; ``GET /login`` renders the form
and ``POST /login`` sets the cookie. See :mod:`veilleur.web.auth` and
:mod:`veilleur.web.login`. Operator-facing form endpoints live under
``/ui`` plus a top-level ``/`` index.
"""

from veilleur.web.login import login_router
from veilleur.web.routes import (
    STATIC_DIR,
    STATIC_PATH,
    web_router,
)

__all__ = ["STATIC_DIR", "STATIC_PATH", "login_router", "web_router"]
