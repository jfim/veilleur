"""Application logging configuration.

Veilleur otherwise installs no logging handlers, so records fall through to
Python's last-resort handler: bare ``%(message)s`` to stderr, and only at
WARNING or above. That is why INFO-level diagnostics (the xpath-derivation
trace, LLM request/retry lines) never showed up in deployed logs, and why
warnings appeared with no level/timestamp prefix.

:func:`configure_logging` scopes a proper formatter to the ``veilleur``
package logger and applies the configured level. It deliberately attaches to
``veilleur`` rather than the root logger so it doesn't fight uvicorn's own
handlers and keeps propagation intact (so test log-capture still works).
"""

from __future__ import annotations

import logging
import sys

HANDLER_NAME = "veilleur-default"
_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"


def _resolve_level(level: str) -> int:
    return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)


def configure_logging(level: str = "INFO") -> None:
    """Attach a stderr handler + formatter to the ``veilleur`` logger.

    Idempotent: re-applies the level on every call but never stacks a second
    handler, so it is safe to invoke on each application startup.
    """
    resolved = _resolve_level(level)
    logger = logging.getLogger("veilleur")
    logger.setLevel(resolved)

    for handler in logger.handlers:
        if handler.name == HANDLER_NAME:
            handler.setLevel(resolved)
            return

    handler = logging.StreamHandler(sys.stderr)
    handler.name = HANDLER_NAME
    handler.setLevel(resolved)
    handler.setFormatter(logging.Formatter(_FORMAT))
    logger.addHandler(handler)
