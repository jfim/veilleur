"""XPath-derivation prompt: default text plus a file-backed override.

The prompt that drives :func:`veilleur.xpath.derive.derive_xpath` ships
with a sensible default, but operators routinely want to tweak it
without rebuilding the image. Convention:

- ``settings.PROMPT_FILE`` (env: ``PROMPT_FILE``)
  optionally points at an override path. When unset, the default is
  used unconditionally.
- File **absence** means "use the bundled default". File **presence**
  means "use the contents of this file as the template".
- Saving content that matches the bundled default removes the override
  file, so the next default-prompt upgrade flows through automatically.
- Writes are atomic: temp-file + ``os.replace`` so a crash mid-save
  cannot leave a truncated prompt on disk.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from veilleur.config import get_settings

DEFAULT_PROMPT_TEMPLATE = """Given the following links from a webpage, return an xpath expression that matches only links to articles. Ignore navigation, tag/category, pagination, and other non-article links — only links a reader would treat as an article, suitable for an RSS feed. The page is "{title}" from {url}.

Each line below describes one <a> element with a numeric id, a CSS-like descriptive path, the anchor text, and the raw href. The path uses `tag#id` when the element has an id, `tag.class1.class2` for classes, `tag#id.class` for both, and `tag[N]` (1-indexed among same-tag siblings) only when neither id nor class is available. Prefer xpath predicates that target ids and class names over positional indices.

Hrefs are shown raw — relative hrefs like `2025/post/index.html` won't match `contains(@href, '/2025/')`; use `starts-with(@href, '2025/')` or key off structure (id/class) instead.

Aim for an xpath specific enough not to match unrelated links added later, but loose enough to keep working as the page evolves. Prefer structural anchors (containers identified by id or class) and stable attribute predicates over volatile ones like year or month substrings or positional indices. For example, `//div[3]//a[contains(@href, '/2026/')]` will silently break after 2026 and is fragile to layout changes; `//div[contains(@class,'post-list')]//a[not(contains(@href, '/tags'))]` is more robust because it keys on a stable structural class and only excludes a known irrelevant section.

Links:
{listing}
{previous_attempts}
Reply with exactly two lines and nothing else — no commentary, no markdown, no code fences:

  articles: <comma-separated list of ids you intend to match>
  xpath: <the xpath expression>

If no xpath can match all article links and only article links, reply with the single word `unable` on its own line instead.
"""


def prompt_override_path() -> Path | None:
    """Configured override path, or ``None`` if no override is configured."""
    return get_settings().PROMPT_FILE


def _normalize(text: str) -> str:
    """Normalize for default-equality comparison only.

    A round-trip through a ``<textarea>`` typically swaps ``\\n`` for
    ``\\r\\n`` and may strip the trailing newline. We don't want either
    to flip the override on or off, so collapse those before comparing.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def is_default(text: str) -> bool:
    """Return True if ``text`` matches the bundled default after normalization."""
    return _normalize(text) == _normalize(DEFAULT_PROMPT_TEMPLATE)


def is_overridden() -> bool:
    """Return True iff an override file is configured *and* exists."""
    path = prompt_override_path()
    return path is not None and path.is_file()


def load_active_template() -> str:
    """Return the template that should drive the next derivation."""
    path = prompt_override_path()
    if path is None:
        return DEFAULT_PROMPT_TEMPLATE
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_PROMPT_TEMPLATE


def save_template(text: str) -> bool:
    """Persist ``text`` as the active template.

    If ``text`` matches the default, the override file is removed (or
    left absent) so future default-prompt upgrades take effect. Otherwise
    the file is created/overwritten atomically.

    Returns True if an override file is present after the call, False
    if the system is now back on the default.

    Raises:
        RuntimeError: If ``PROMPT_FILE`` is not configured.
    """
    path = prompt_override_path()
    if path is None:
        raise RuntimeError(
            "PROMPT_FILE is not configured; cannot persist prompt overrides"
        )
    if is_default(text):
        reset_to_default()
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling temp file, then atomic-rename into place.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return True


def reset_to_default() -> None:
    """Remove the override file if it exists. No-op when already on the default."""
    path = prompt_override_path()
    if path is None:
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
