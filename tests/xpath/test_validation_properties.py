"""Hypothesis property tests for ``veilleur.xpath.validation.validate``.

Covers the invariants from §11.2 of ``docs/design/phase-4-diff-validation.md``.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from veilleur.xpath.validation import validate

# ---------------------------------------------------------------------------
# URL strategies
# ---------------------------------------------------------------------------

# Plain non-numeric segment text (so we can mix in numeric segments by hand
# and reason about which is which).
_letters = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8)

# A path segment may be numeric, non-numeric, or numeric-with-suffix.
_segment = st.one_of(
    _letters,
    st.integers(min_value=0, max_value=99999).map(str),
    st.tuples(_letters, st.integers(min_value=0, max_value=99)).map(lambda t: f"{t[0]}-{t[1]}"),
)

_host = st.sampled_from(["ex.com", "example.org", "blog.test"])


@st.composite
def url_strat(draw: st.DrawFn, host: str | None = None) -> str:
    h = host if host is not None else draw(_host)
    n_segs = draw(st.integers(min_value=0, max_value=4))
    segs = [draw(_segment) for _ in range(n_segs)]
    path = "/" + "/".join(segs) if segs else ""
    return f"https://{h}{path}"


@st.composite
def url_list_same_host(draw: st.DrawFn, min_size: int = 0, max_size: int = 5) -> list[str]:
    host = draw(_host)
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [draw(url_strat(host=host)) for _ in range(n)]


# ---------------------------------------------------------------------------
# 1. Purity / determinism.
# ---------------------------------------------------------------------------


@given(prev=url_list_same_host(), new=url_list_same_host())
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_purity(prev: list[str], new: list[str]) -> None:
    assert validate(prev, new) == validate(prev, new)


# ---------------------------------------------------------------------------
# 2. Order independence of prev (variant + prefix).
# ---------------------------------------------------------------------------


@given(prev=url_list_same_host(min_size=1), new=url_list_same_host(min_size=1))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_prev_order_independence(prev: list[str], new: list[str]) -> None:
    a = validate(prev, new)
    b = validate(list(reversed(prev)), new)
    assert a.kind == b.kind
    # prefix (when present) should be invariant under permutation of prev.
    pa = getattr(a, "prefix", None)
    pb = getattr(b, "prefix", None)
    assert pa == pb


# ---------------------------------------------------------------------------
# 3. Order independence of new (variant + matched/unmatched as sets).
# ---------------------------------------------------------------------------


@given(prev=url_list_same_host(min_size=1), new=url_list_same_host(min_size=1))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_new_order_independence(prev: list[str], new: list[str]) -> None:
    a = validate(prev, new)
    b = validate(prev, list(reversed(new)))
    assert a.kind == b.kind
    assert set(getattr(a, "matched", ())) == set(getattr(b, "matched", ()))
    assert set(getattr(a, "unmatched", ())) == set(getattr(b, "unmatched", ()))


# ---------------------------------------------------------------------------
# 4. Idempotence under query/fragment noise.
# ---------------------------------------------------------------------------


_noise = st.sampled_from(["", "?utm_source=x", "?fbclid=abc&utm=y", "#section"])


@given(
    prev=url_list_same_host(min_size=1),
    new=url_list_same_host(min_size=1),
    suffix_prev=st.lists(_noise, min_size=0, max_size=10),
    suffix_new=st.lists(_noise, min_size=0, max_size=10),
)
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_query_fragment_idempotence(
    prev: list[str],
    new: list[str],
    suffix_prev: list[str],
    suffix_new: list[str],
) -> None:
    def with_noise(urls: list[str], suffixes: list[str]) -> list[str]:
        return [u + (suffixes[i % len(suffixes)] if suffixes else "") for i, u in enumerate(urls)]

    a = validate(prev, new)
    b = validate(with_noise(prev, suffix_prev), with_noise(new, suffix_new))
    assert a.kind == b.kind


# ---------------------------------------------------------------------------
# 5. Self-match: validate(prev, prev) is Ok when prev is non-empty,
#    single-host (guaranteed by the strategy).
# ---------------------------------------------------------------------------


@given(prev=url_list_same_host(min_size=1))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=200)
def test_self_match_is_ok(prev: list[str]) -> None:
    result = validate(prev, prev)
    # Self-match must accept; the resolution removed the "minimum prefix"
    # rule, so even root-level prev is valid. Any prev URL trivially shares
    # its own LCP-derived prefix.
    assert result.kind == "ok", (prev, result)


# ---------------------------------------------------------------------------
# 6. Strict-100% monotonicity: appending a clearly-unmatched URL to an Ok
#    new list flips the result to Regenerate (never Ok, never Fail).
# ---------------------------------------------------------------------------


@given(prev=url_list_same_host(min_size=1))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_strict_threshold(prev: list[str]) -> None:
    # Build a self-match Ok baseline.
    base = validate(prev, prev)
    if base.kind != "ok":
        return  # only meaningful from an Ok baseline
    # Append an obviously-cross-host URL; this must be unmatched.
    polluted = list(prev) + ["https://totally-different-host.invalid/x.html"]
    after = validate(prev, polluted)
    assert after.kind == "regenerate"


# ---------------------------------------------------------------------------
# 7. Empty new with non-empty prev is always Fail.
# ---------------------------------------------------------------------------


@given(prev=url_list_same_host(min_size=1))
@settings(suppress_health_check=[HealthCheck.too_slow], max_examples=100)
def test_empty_new_is_fail(prev: list[str]) -> None:
    result = validate(prev, [])
    assert result.kind == "fail"
