"""Shared types for the xpath subpackage.

Kept separate from :mod:`veilleur.xpath.validation` so that future modules
in this package (e.g. an xpath regenerator) can import the result types
without pulling in the validation logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class NormalizedURL:
    """A URL after normalization, ready for byte-for-byte comparison.

    Query string and fragment are dropped. Scheme and host are lowercased.
    Default ports (80, 443) are stripped. Path segments have empty entries
    (from leading/trailing/repeated slashes) removed.
    """

    scheme: str
    host: str
    port: int | None
    segments: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Ok:
    """All ``new_links`` matched the previous batch's LCP."""

    prefix: tuple[str, ...]
    matched: tuple[str, ...]
    kind: Literal["ok"] = "ok"


@dataclass(frozen=True, slots=True)
class Regenerate:
    """At least one ``new_link`` failed the previous batch's LCP.

    Strict 100% threshold: any unmatched link triggers ``Regenerate``.
    """

    prefix: tuple[str, ...]
    matched: tuple[str, ...]
    unmatched: tuple[str, ...]
    reason: str
    kind: Literal["regenerate"] = "regenerate"


@dataclass(frozen=True, slots=True)
class Fail:
    """A precondition was violated and we cannot meaningfully validate."""

    reason: str
    prefix: tuple[str, ...] | None
    unmatched: tuple[str, ...]
    kind: Literal["fail"] = "fail"


@dataclass(frozen=True, slots=True)
class FirstRun:
    """``prev_links`` was empty: there's nothing to compare against."""

    accepted: tuple[str, ...]
    kind: Literal["first_run"] = "first_run"


ValidationResult = Ok | Regenerate | Fail | FirstRun
