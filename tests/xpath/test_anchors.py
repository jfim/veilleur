"""Tests for ``veilleur.xpath.anchors``."""

from __future__ import annotations

import pytest

from tests.xpath.conftest import load_fixture
from veilleur.xpath import (
    AnchorExtractionError,
    extract_anchors,
)


def test_simple_blog_extracts_title_and_anchors() -> None:
    result = extract_anchors(load_fixture("simple-blog.html"), "https://example.com/")
    assert result.title == "Simple Blog"
    assert result.truncated is False
    assert len(result.anchors) == 4
    assert result.total_before_cap == 4
    hrefs = [a.href for a in result.anchors]
    assert hrefs == ["/", "/posts/first.html", "/posts/second.html", "/about"]
    # Document order preserved.
    assert result.anchors[0].xpath != result.anchors[1].xpath


def test_filter_drops_javascript_mailto_tel_fragments_empty() -> None:
    result = extract_anchors(load_fixture("js-mailto.html"), "https://example.com/")
    assert len(result.anchors) == 1
    assert result.anchors[0].href == "/posts/keep-me.html"


def test_text_truncation_at_120_chars() -> None:
    long = "x" * 200
    html = f"<html><head><title>T</title></head><body><a href='/p'>{long}</a></body></html>"
    result = extract_anchors(html, "https://example.com/")
    assert len(result.anchors) == 1
    text = result.anchors[0].text
    assert len(text) == 120
    assert text.endswith("...")


def test_whitespace_collapsed_in_text() -> None:
    html = """
    <html><head><title>T</title></head>
    <body><a href='/p'>  hello   world  \n\t  bye </a></body></html>
    """
    result = extract_anchors(html, "https://example.com/")
    assert result.anchors[0].text == "hello world bye"


def test_no_anchors_raises() -> None:
    html = "<html><head><title>T</title></head><body><p>nothing</p></body></html>"
    with pytest.raises(AnchorExtractionError):
        extract_anchors(html, "https://example.com/")


def test_only_useless_anchors_raises() -> None:
    html = """<html><head><title>T</title></head><body>
    <a href="javascript:void(0)">x</a>
    <a href="#">y</a>
    </body></html>"""
    with pytest.raises(AnchorExtractionError):
        extract_anchors(html, "https://example.com/")


def test_missing_title_falls_back() -> None:
    html = "<html><body><a href='/p'>x</a></body></html>"
    result = extract_anchors(html, "https://example.com/")
    assert result.title == "(untitled)"


def test_empty_title_tag_falls_back() -> None:
    html = "<html><head><title>   </title></head><body><a href='/p'>x</a></body></html>"
    result = extract_anchors(html, "https://example.com/")
    assert result.title == "(untitled)"


def test_malformed_html_still_parses() -> None:
    result = extract_anchors(load_fixture("malformed.html"), "https://example.com/")
    assert result.title == "Malformed"
    hrefs = [a.href for a in result.anchors]
    assert "/posts/m1.html" in hrefs
    assert "/posts/m2.html" in hrefs


def test_anchor_xpath_is_absolute() -> None:
    result = extract_anchors(load_fixture("simple-blog.html"), "https://example.com/")
    for anchor in result.anchors:
        assert anchor.xpath.startswith("/")


def test_descriptive_path_uses_id_when_present() -> None:
    html = (
        "<html><head><title>T</title></head>"
        "<body><div id='content'><a href='/p'>x</a></div></body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    assert result.anchors[0].xpath == "/html/body/div#content/a"


def test_descriptive_path_uses_class_when_no_id() -> None:
    html = (
        "<html><head><title>T</title></head>"
        "<body><div class='post-list wide'><a href='/p'>x</a></div></body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    assert result.anchors[0].xpath == "/html/body/div.post-list.wide/a"


def test_descriptive_path_uses_id_and_class_when_both_present() -> None:
    html = (
        "<html><head><title>T</title></head>"
        "<body><div id='main' class='post-list'><a href='/p'>x</a></div></body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    assert result.anchors[0].xpath == "/html/body/div#main.post-list/a"


def test_descriptive_path_falls_back_to_positional_among_same_tag() -> None:
    html = (
        "<html><head><title>T</title></head><body>"
        "<div><p>a</p></div>"
        "<div><a href='/p'>x</a></div>"
        "<div><span>z</span></div>"
        "</body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    # Anchor sits inside the second <div> sibling.
    assert result.anchors[0].xpath == "/html/body/div[2]/a"


def test_descriptive_path_omits_index_when_unique_among_siblings() -> None:
    html = (
        "<html><head><title>T</title></head><body>"
        "<header><a href='/h'>h</a></header>"
        "<main><a href='/m'>m</a></main>"
        "</body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    paths = [a.xpath for a in result.anchors]
    assert paths == ["/html/body/header/a", "/html/body/main/a"]


def test_descriptive_path_caps_classes_per_segment() -> None:
    html = (
        "<html><head><title>T</title></head>"
        "<body><div class='a b c d e f'><a href='/p'>x</a></div></body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    assert result.anchors[0].xpath == "/html/body/div.a.b.c/a"


def test_anchor_cap_truncates_and_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEILLEUR_XPATH_MAX_ANCHORS", "3")
    html = (
        "<html><head><title>T</title></head><body>"
        + "".join(f"<a href='/p{i}'>p{i}</a>" for i in range(10))
        + "</body></html>"
    )
    result = extract_anchors(html, "https://example.com/")
    assert result.truncated is True
    assert result.total_before_cap == 10
    assert len(result.anchors) == 3
    assert [a.href for a in result.anchors] == ["/p0", "/p1", "/p2"]


def test_anchor_cap_invalid_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEILLEUR_XPATH_MAX_ANCHORS", "not-a-number")
    with pytest.raises(AnchorExtractionError):
        extract_anchors(load_fixture("simple-blog.html"), "https://example.com/")


def test_anchor_cap_zero_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEILLEUR_XPATH_MAX_ANCHORS", "0")
    with pytest.raises(AnchorExtractionError):
        extract_anchors(load_fixture("simple-blog.html"), "https://example.com/")


def test_base_url_required() -> None:
    with pytest.raises(AnchorExtractionError):
        extract_anchors("<html><body><a href='/p'>x</a></body></html>", "")


def test_the_batch_fixture_yields_many_anchors() -> None:
    result = extract_anchors(
        load_fixture("the-batch.html"), "https://www.deeplearning.ai/the-batch/"
    )
    assert result.title  # whatever the page has, not empty
    assert len(result.anchors) > 10
