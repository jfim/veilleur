"""Shared fixtures for xpath tests."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class FakeLLMClient:
    """In-memory LLMClient implementation for unit tests.

    Pops one canned reply per call and records the prompts it received.
    """

    def __init__(self, replies: str | Iterable[str]) -> None:
        if isinstance(replies, str):
            self._replies: list[str] = [replies]
        else:
            self._replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._replies:
            raise AssertionError("FakeLLMClient ran out of canned replies")
        return self._replies.pop(0)


def load_fixture(name: str) -> str:
    """Read a fixture HTML file from ``tests/xpath/fixtures/``."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


@pytest.fixture
def fixture_loader():
    """Pytest fixture exposing :func:`load_fixture`."""
    return load_fixture
