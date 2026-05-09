"""Table-driven tests for ``veilleur.xpath.validation.validate``.

Covers the 20 edge cases from ``docs/design/phase-4-diff-validation.md``,
adjusted for the phase-4 resolution that removed the "minimum prefix" rule.
"""

from __future__ import annotations

import pytest

from veilleur.xpath.validation import (
    is_numeric_segment,
    lcp,
    normalize,
    shares_prefix,
    validate,
)

# ---------------------------------------------------------------------------
# validate() — table-driven decision-tree cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,prev,new,expected_kind",
    [
        # 1. First run — empty prev with non-empty new.
        (1, [], ["https://ex.com/a"], "first_run"),
        # 2. First run — both empty.
        (2, [], [], "first_run"),
        # 3. Empty new with non-empty prev -> Fail.
        (3, ["https://ex.com/a"], [], "fail"),
        # 4. Mixed hosts in prev -> Fail.
        (
            4,
            ["https://hostA.com/x", "https://hostB.com/y"],
            ["https://hostA.com/z"],
            "fail",
        ),
        # 5. Two-prev, third-link in same year -> Ok.
        (
            5,
            [
                "https://ex.com/posts/2026/a.html",
                "https://ex.com/posts/2026/b.html",
            ],
            ["https://ex.com/posts/2026/c.html"],
            "ok",
        ),
        # 6. Two-prev, link in a *different* year (wildcard at pos 1) -> Ok.
        (
            6,
            [
                "https://ex.com/posts/2026/a.html",
                "https://ex.com/posts/2026/b.html",
            ],
            ["https://ex.com/posts/2027/c.html"],
            "ok",
        ),
        # 7. Two-prev, non-numeric where wildcard expected -> Regenerate.
        (
            7,
            [
                "https://ex.com/posts/2026/a.html",
                "https://ex.com/posts/2026/b.html",
            ],
            ["https://ex.com/posts/about"],
            "regenerate",
        ),
        # 8. Two-prev, candidate too short for prefix -> Regenerate.
        (
            8,
            [
                "https://ex.com/posts/2026/a.html",
                "https://ex.com/posts/2026/b.html",
            ],
            ["https://ex.com/something.html"],
            "regenerate",
        ),
        # 9. Mixed matched+unmatched -> Regenerate (strict 100%).
        (
            9,
            [
                "https://ex.com/posts/2026/a.html",
                "https://ex.com/posts/2026/b.html",
            ],
            [
                "https://ex.com/posts/2026/c.html",
                "https://ex.com/x.html",
            ],
            "regenerate",
        ),
        # 10. Single-prev (directory match) -> Ok.
        (
            10,
            ["https://ex.com/posts/2026/a.html"],
            ["https://ex.com/posts/2026/b.html"],
            "ok",
        ),
        # 11. Single-prev: literal segments, different numeric -> Regenerate.
        (
            11,
            ["https://ex.com/posts/2026/a.html"],
            ["https://ex.com/posts/2027/b.html"],
            "regenerate",
        ),
        # 12. Numeric-only directory — Ok per resolution (no minimum prefix).
        (
            12,
            ["https://ex.com/2026/a.html"],
            ["https://ex.com/2026/b.html"],
            "ok",
        ),
        # 13. Mixed-case scheme/host — normalization makes it Ok.
        (
            13,
            ["HTTPS://EX.COM/p/1.html", "https://ex.com/p/2.html"],
            ["https://ex.com/p/3.html"],
            "ok",
        ),
        # 14. Query string differs only -> dropped -> Ok.
        (
            14,
            ["https://ex.com/p/a?x=1"],
            ["https://ex.com/p/a?x=2"],
            "ok",
        ),
        # 15. Fragment differs only -> dropped -> Ok.
        (
            15,
            ["https://ex.com/p/a#h1"],
            ["https://ex.com/p/a#h2"],
            "ok",
        ),
        # 16. Default port stripped.
        (
            16,
            ["https://ex.com:443/p/a", "https://ex.com/p/b"],
            ["https://ex.com/p/c"],
            "ok",
        ),
        # 17. Repeated slashes collapsed.
        (
            17,
            ["https://ex.com//p///a"],
            ["https://ex.com/p/a"],
            "ok",
        ),
        # 18. Numeric-tail wildcard — different value still Ok.
        (
            18,
            ["https://ex.com/blog/2026", "https://ex.com/blog/2027"],
            ["https://ex.com/blog/2028"],
            "ok",
        ),
        # 19. Numeric-tail wildcard — non-numeric replacement -> Regenerate.
        (
            19,
            ["https://ex.com/blog/2026", "https://ex.com/blog/2027"],
            ["https://ex.com/blog/about"],
            "regenerate",
        ),
        # 20. LCP empty (no shared non-numeric segments) — Ok per resolution
        # (the empty prefix is legal for root-level feeds).
        (
            20,
            ["https://ex.com/a/1", "https://ex.com/b/2"],
            ["https://ex.com/a/3"],
            "ok",
        ),
    ],
)
def test_validate_table(case_id: int, prev: list[str], new: list[str], expected_kind: str) -> None:
    result = validate(prev, new)
    assert result.kind == expected_kind, (
        f"case {case_id}: got {result.kind}, expected {expected_kind} ({result!r})"
    )


# ---------------------------------------------------------------------------
# Worked example from README — must be Ok.
# ---------------------------------------------------------------------------


def test_readme_worked_example_ok() -> None:
    prev = [
        "https://example.com/posts/2026/mypost.html",
        "https://example.com/posts/2026/another.html",
    ]
    new = [
        "https://example.com/posts/2026/third.html",
        "https://example.com/posts/2026/fourth.html",
    ]
    result = validate(prev, new)
    assert result.kind == "ok"
    assert result.prefix == ("posts", "*")  # type: ignore[union-attr]
    assert set(result.matched) == set(new)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# normalize()
# ---------------------------------------------------------------------------


def test_normalize_lowercases_scheme_and_host() -> None:
    n = normalize("HTTPS://EXAMPLE.COM/Foo/Bar")
    assert n.scheme == "https"
    assert n.host == "example.com"
    assert n.segments == ("Foo", "Bar")  # path case is preserved


def test_normalize_drops_default_ports() -> None:
    assert normalize("http://ex.com:80/x").port is None
    assert normalize("https://ex.com:443/x").port is None
    assert normalize("https://ex.com:8443/x").port == 8443


def test_normalize_collapses_slashes_and_drops_query_fragment() -> None:
    n = normalize("https://ex.com//a///b/?x=1#frag")
    assert n.segments == ("a", "b")


def test_normalize_root_path() -> None:
    n = normalize("https://ex.com")
    assert n.segments == ()
    n2 = normalize("https://ex.com/")
    assert n2.segments == ()


# ---------------------------------------------------------------------------
# is_numeric_segment()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("2026", True),
        ("2026-05", True),
        ("page-3", True),
        ("post-12345", True),
        ("v1.2", True),
        ("posts", False),
        ("articles", False),
        ("mypost", False),
        ("", False),
    ],
)
def test_is_numeric_segment(segment: str, expected: bool) -> None:
    assert is_numeric_segment(segment) is expected


# ---------------------------------------------------------------------------
# lcp()
# ---------------------------------------------------------------------------


def test_lcp_empty() -> None:
    assert lcp([]) == ()


def test_lcp_single_input_drops_filename() -> None:
    assert lcp([("blog", "2026", "post-1.html")]) == ("blog", "2026")


def test_lcp_two_inputs_with_numeric_position() -> None:
    assert lcp([("posts", "2026", "a.html"), ("posts", "2026", "b.html")]) == ("posts", "*")


def test_lcp_terminates_on_non_numeric_mismatch() -> None:
    assert lcp([("a", "x"), ("b", "y")]) == ()


# ---------------------------------------------------------------------------
# shares_prefix()
# ---------------------------------------------------------------------------


def test_shares_prefix_wildcard_requires_numeric() -> None:
    assert shares_prefix(("posts", "2026", "x"), ("posts", "*"))
    assert not shares_prefix(("posts", "about"), ("posts", "*"))


def test_shares_prefix_too_short() -> None:
    assert not shares_prefix(("posts",), ("posts", "*"))


def test_shares_prefix_empty_prefix_matches_anything_same_host() -> None:
    assert shares_prefix(("anything", "here"), ())
    assert shares_prefix((), ())


# ---------------------------------------------------------------------------
# Cross-host new link.
# ---------------------------------------------------------------------------


def test_cross_host_new_link_is_unmatched() -> None:
    prev = [
        "https://ex.com/p/2026/a.html",
        "https://ex.com/p/2026/b.html",
    ]
    new = ["https://cdn.ex.com/p/2026/c.html"]
    result = validate(prev, new)
    assert result.kind == "regenerate"
    assert result.unmatched == tuple(new)  # type: ignore[union-attr]
