"""Tests for the file-backed prompt override in :mod:`veilleur.xpath.prompt`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from veilleur.config import get_settings
from veilleur.xpath import prompt as xpath_prompt
from veilleur.xpath.derive import render_prompt
from veilleur.xpath.types import Anchor


@pytest.fixture
def configured_prompt_file(tmp_path: Path) -> Iterator[Path]:
    """Point ``VEILLEUR_PROMPT_FILE`` at a temp path for the duration of a test."""
    target = tmp_path / "prompt.txt"
    old = os.environ.get("VEILLEUR_PROMPT_FILE")
    os.environ["VEILLEUR_PROMPT_FILE"] = str(target)
    get_settings.cache_clear()
    try:
        yield target
    finally:
        if old is None:
            os.environ.pop("VEILLEUR_PROMPT_FILE", None)
        else:
            os.environ["VEILLEUR_PROMPT_FILE"] = old
        get_settings.cache_clear()


@pytest.fixture
def unconfigured_prompt_file() -> Iterator[None]:
    old = os.environ.pop("VEILLEUR_PROMPT_FILE", None)
    get_settings.cache_clear()
    try:
        yield
    finally:
        if old is not None:
            os.environ["VEILLEUR_PROMPT_FILE"] = old
        get_settings.cache_clear()


def test_load_returns_default_when_unconfigured(unconfigured_prompt_file: None) -> None:
    assert xpath_prompt.load_active_template() == xpath_prompt.DEFAULT_PROMPT_TEMPLATE
    assert xpath_prompt.is_overridden() is False


def test_load_returns_default_when_file_absent(configured_prompt_file: Path) -> None:
    assert not configured_prompt_file.exists()
    assert xpath_prompt.load_active_template() == xpath_prompt.DEFAULT_PROMPT_TEMPLATE
    assert xpath_prompt.is_overridden() is False


def test_save_creates_override_file(configured_prompt_file: Path) -> None:
    custom = "Custom prompt {title} {url}\n{listing}\n"
    overridden = xpath_prompt.save_template(custom)
    assert overridden is True
    assert configured_prompt_file.read_text(encoding="utf-8") == custom
    assert xpath_prompt.load_active_template() == custom
    assert xpath_prompt.is_overridden() is True


def test_save_default_text_unlinks_override(configured_prompt_file: Path) -> None:
    xpath_prompt.save_template("an override that exists")
    assert configured_prompt_file.exists()

    overridden = xpath_prompt.save_template(xpath_prompt.DEFAULT_PROMPT_TEMPLATE)
    assert overridden is False
    assert not configured_prompt_file.exists()
    assert xpath_prompt.load_active_template() == xpath_prompt.DEFAULT_PROMPT_TEMPLATE


def test_save_default_text_with_crlf_unlinks_override(configured_prompt_file: Path) -> None:
    xpath_prompt.save_template("override")
    crlf = xpath_prompt.DEFAULT_PROMPT_TEMPLATE.replace("\n", "\r\n").rstrip()
    overridden = xpath_prompt.save_template(crlf)
    assert overridden is False
    assert not configured_prompt_file.exists()


def test_reset_to_default_removes_file(configured_prompt_file: Path) -> None:
    xpath_prompt.save_template("override")
    assert configured_prompt_file.exists()
    xpath_prompt.reset_to_default()
    assert not configured_prompt_file.exists()


def test_reset_to_default_no_op_when_absent(configured_prompt_file: Path) -> None:
    assert not configured_prompt_file.exists()
    xpath_prompt.reset_to_default()
    assert not configured_prompt_file.exists()


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    nested = tmp_path / "config" / "prompts" / "active.txt"
    os.environ["VEILLEUR_PROMPT_FILE"] = str(nested)
    get_settings.cache_clear()
    try:
        xpath_prompt.save_template("nested override")
        assert nested.read_text(encoding="utf-8") == "nested override"
    finally:
        os.environ.pop("VEILLEUR_PROMPT_FILE", None)
        get_settings.cache_clear()


def test_save_raises_when_unconfigured(unconfigured_prompt_file: None) -> None:
    with pytest.raises(RuntimeError, match="VEILLEUR_PROMPT_FILE"):
        xpath_prompt.save_template("anything")


def test_render_prompt_uses_override(configured_prompt_file: Path) -> None:
    xpath_prompt.save_template("CUSTOM:{title}|{url}\n{listing}")
    rendered = render_prompt(
        "Page",
        "https://example.com/",
        [Anchor(xpath="/a", text="x", href="https://example.com/x")],
    )
    assert rendered.startswith("CUSTOM:Page|https://example.com/")
    assert "/a | text='x' | href=https://example.com/x" in rendered
