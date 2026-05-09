"""Tests for the gzipped on-disk raw-HTML store."""

from __future__ import annotations

import gzip
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from veilleur.scraper import read_html, validate_raw_html_dir, write_html


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    feed_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    run_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    when = datetime(2026, 5, 9, 12, 34, 56, tzinfo=UTC)
    html = "<html><body>hello world</body></html>"

    rel = write_html(
        raw_html_dir=tmp_path,
        feed_id=feed_id,
        run_id=run_id,
        when=when,
        html=html,
    )

    expected_rel = (
        f"{feed_id}/2026/05/2026-05-09-12-34-56-{run_id.hex[:8]}.html.gz"
    )
    assert rel == expected_rel

    target = tmp_path / rel
    assert target.exists()
    # Verify it really is gzip on disk.
    with gzip.open(target, "rb") as fh:
        assert fh.read().decode("utf-8") == html

    assert read_html(raw_html_dir=tmp_path, stored_path=rel) == html


def test_validate_rejects_relative_path() -> None:
    with pytest.raises(RuntimeError, match="absolute"):
        validate_raw_html_dir(Path("relative/path"))


def test_validate_creates_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "new" / "nested" / "dir"
    validate_raw_html_dir(target)
    assert target.is_dir()


def test_validate_rejects_unwritable_dir(tmp_path: Path) -> None:
    target = tmp_path / "ro"
    target.mkdir()
    target.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            validate_raw_html_dir(target)
    finally:
        target.chmod(0o700)
