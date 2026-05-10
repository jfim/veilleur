"""Filesystem storage for raw fetched HTML.

When ``Settings.RAW_HTML_DIR`` is configured the scrape pipeline
gzips each successfully-fetched page and writes it under that directory.
A relative path (rooted at the configured directory) is then stored on the
``scrape_runs`` row in ``raw_html_path``; the in-DB ``raw_html`` column is
left ``NULL``.

Layout::

    {raw_html_dir}/{feed_uuid}/{year}/{month}/{yyyy-mm-dd-hh-mm-ss}-{run_short}.html.gz

``run_short`` is the first 8 hex characters of the scrape-run UUID (with the
hyphens stripped) so that two runs that happen to share the same wall-clock
second cannot collide and so that path-to-row lookup is trivial.
"""

from __future__ import annotations

import gzip
import uuid
from datetime import datetime
from pathlib import Path


def validate_raw_html_dir(path: Path) -> None:
    """Validate the configured raw-HTML directory at startup.

    Raises ``RuntimeError`` (with a clear message) if the path is not
    absolute, cannot be created, or is not writable.
    """
    if not path.is_absolute():
        raise RuntimeError(f"RAW_HTML_DIR must be an absolute path, got: {path!s}")
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"RAW_HTML_DIR {path!s} could not be created: {exc}") from exc
    # Probe writability with a temp file.
    probe = path / ".veilleur-write-probe"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"RAW_HTML_DIR {path!s} is not writable: {exc}") from exc


def _relative_path(*, feed_id: uuid.UUID, run_id: uuid.UUID, when: datetime) -> Path:
    """Compute the storage path (relative to ``raw_html_dir``)."""
    run_short = run_id.hex[:8]
    ts = when.strftime("%Y-%m-%d-%H-%M-%S")
    return (
        Path(str(feed_id)) / f"{when.year:04d}" / f"{when.month:02d}" / f"{ts}-{run_short}.html.gz"
    )


def write_html(
    *,
    raw_html_dir: Path,
    feed_id: uuid.UUID,
    run_id: uuid.UUID,
    when: datetime,
    html: str,
) -> str:
    """Gzip ``html`` and write it under ``raw_html_dir``.

    Returns the path stored in ``scrape_runs.raw_html_path`` — currently the
    relative path within ``raw_html_dir`` so that re-rooting the storage
    directory does not invalidate existing rows.
    """
    rel = _relative_path(feed_id=feed_id, run_id=run_id, when=when)
    target = raw_html_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wb") as fh:
        fh.write(html.encode("utf-8"))
    return str(rel)


def read_html(*, raw_html_dir: Path, stored_path: str) -> str:
    """Read and gunzip a previously-stored HTML payload.

    ``stored_path`` is whatever ``write_html`` returned (a relative path).
    """
    target = raw_html_dir / stored_path
    with gzip.open(target, "rb") as fh:
        return fh.read().decode("utf-8")
