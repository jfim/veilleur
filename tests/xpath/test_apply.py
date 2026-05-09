"""Tests for ``veilleur.xpath.apply``."""

from __future__ import annotations

import pytest

from tests.xpath.conftest import load_fixture
from veilleur.xpath import (
    AnchorExtractionError,
    XPathNoMatchError,
    XPathSyntaxError,
    XPathWrongElementError,
    apply_xpath,
)


def test_simple_blog_xpath_matches_posts() -> None:
    items = apply_xpath(
        load_fixture("simple-blog.html"),
        "https://example.com/",
        "//ul[@class='posts']/li/a",
    )
    assert len(items) == 2
    assert items[0].url == "https://example.com/posts/first.html"
    assert items[0].title == "First Post"
    assert items[1].url == "https://example.com/posts/second.html"
    assert all(i.source_xpath == "//ul[@class='posts']/li/a" for i in items)


def test_relative_urls_absolutized() -> None:
    items = apply_xpath(
        load_fixture("relative-urls.html"),
        "https://example.com/blog/index.html",
        "//ul/li/a",
    )
    urls = [i.url for i in items]
    assert "https://example.com/blog/post-a.html" in urls
    assert "https://example.com/abs/post-b.html" in urls
    assert "https://other.example.com/post-c.html" in urls
    assert "https://example.com/up/post-d.html" in urls


def test_fragments_stripped() -> None:
    items = apply_xpath(
        load_fixture("fragments.html"),
        "https://example.com/",
        "//ul/li/a",
    )
    assert [i.url for i in items] == [
        "https://example.com/posts/one.html",
        "https://example.com/posts/two.html",
    ]


def test_dedupe_first_occurrence_wins() -> None:
    items = apply_xpath(
        load_fixture("dupes.html"),
        "https://example.com/",
        "//ul/li/a",
    )
    assert [i.url for i in items] == [
        "https://example.com/posts/one.html",
        "https://example.com/posts/two.html",
    ]
    # Title comes from the first occurrence.
    assert items[0].title == "One (first)"


def test_empty_text_falls_back_to_url_segment() -> None:
    items = apply_xpath(
        load_fixture("empty-text.html"),
        "https://example.com/",
        "//ul/li/a",
    )
    titles = {i.url: i.title for i in items}
    assert titles["https://example.com/posts/no-text-here.html"] == "no-text-here.html"
    assert titles["https://example.com/posts/has-text.html"] == "Has text"
    assert titles["https://example.com/posts/whitespace-only.html"] == "whitespace-only.html"


def test_invalid_xpath_raises_syntax() -> None:
    with pytest.raises(XPathSyntaxError):
        apply_xpath(
            load_fixture("simple-blog.html"),
            "https://example.com/",
            "//[broken",
        )


def test_no_match_raises() -> None:
    with pytest.raises(XPathNoMatchError):
        apply_xpath(
            load_fixture("simple-blog.html"),
            "https://example.com/",
            "//a[@class='does-not-exist']",
        )


def test_non_anchor_match_raises() -> None:
    with pytest.raises(XPathWrongElementError):
        apply_xpath(
            load_fixture("simple-blog.html"),
            "https://example.com/",
            "//ul[@class='posts']",
        )


def test_mixed_anchor_and_non_anchor_raises() -> None:
    # Match all elements with href OR all li elements — gets non-<a>.
    with pytest.raises(XPathWrongElementError):
        apply_xpath(
            load_fixture("simple-blog.html"),
            "https://example.com/",
            "//ul[@class='posts']/li",
        )


def test_base_url_required() -> None:
    with pytest.raises(AnchorExtractionError):
        apply_xpath("<html></html>", "", "//a")


def test_unparseable_html_raises() -> None:
    # lxml is very tolerant; an empty string is the realistic failure.
    with pytest.raises(AnchorExtractionError):
        apply_xpath("", "https://example.com/", "//a")


def test_mixed_nav_xpath_can_select_only_articles() -> None:
    items = apply_xpath(
        load_fixture("mixed-nav.html"),
        "https://example.com/",
        "//main/article/a",
    )
    assert len(items) == 3
    for item in items:
        assert "/articles/" in item.url
