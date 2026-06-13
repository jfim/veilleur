"""Tests for application logging configuration."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from veilleur.logging_setup import HANDLER_NAME, configure_logging


@pytest.fixture
def restore_veilleur_logger() -> Iterator[logging.Logger]:
    """Snapshot/restore the ``veilleur`` logger so tests stay hermetic."""
    logger = logging.getLogger("veilleur")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield logger
    finally:
        logger.handlers = saved_handlers
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def test_configure_logging_enables_info_with_formatter(
    restore_veilleur_logger: logging.Logger,
) -> None:
    logger = restore_veilleur_logger
    logger.handlers = []

    configure_logging("INFO")

    assert logger.level == logging.INFO
    assert logging.getLogger("veilleur.xpath.derive").isEnabledFor(logging.INFO)
    ours = [h for h in logger.handlers if h.name == HANDLER_NAME]
    assert len(ours) == 1
    assert ours[0].formatter is not None


def test_configure_logging_is_idempotent(
    restore_veilleur_logger: logging.Logger,
) -> None:
    logger = restore_veilleur_logger
    logger.handlers = []

    configure_logging("INFO")
    configure_logging("DEBUG")

    ours = [h for h in logger.handlers if h.name == HANDLER_NAME]
    assert len(ours) == 1  # no duplicate handler on re-config
    assert logger.level == logging.DEBUG  # level still updated


def test_configure_logging_unknown_level_defaults_to_info(
    restore_veilleur_logger: logging.Logger,
) -> None:
    logger = restore_veilleur_logger
    logger.handlers = []

    configure_logging("NOPE")

    assert logger.level == logging.INFO
