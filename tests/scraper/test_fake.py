"""Tests for ``FakePassePartout`` — exercises every error path."""

from __future__ import annotations

import pytest

from veilleur.scraper import (
    FakePassePartout,
    FetchTimeout,
    PassePartoutAuthError,
    PassePartoutBusy,
    PassePartoutUnavailable,
    UnsupportedContentType,
)


async def test_fake_success() -> None:
    fake = FakePassePartout()
    fake.register("https://example.com", html="<html><body>hi</body></html>", status=200)
    result = await fake.fetch("https://example.com")
    assert result.html == "<html><body>hi</body></html>"
    assert result.status_code == 200
    assert result.final_url == "https://example.com"
    assert result.elapsed_ms == 5
    assert fake.calls == ["https://example.com"]


async def test_fake_target_404_returns_status_does_not_raise() -> None:
    fake = FakePassePartout()
    fake.register("https://404.example", html="<h1>not found</h1>", status=404)
    result = await fake.fetch("https://404.example")
    assert result.status_code == 404
    assert "not found" in result.html


async def test_fake_timeout_no_partial() -> None:
    fake = FakePassePartout()
    fake.register_timeout("https://slow.example")
    with pytest.raises(FetchTimeout) as exc_info:
        await fake.fetch("https://slow.example")
    assert exc_info.value.partial is None


async def test_fake_unavailable() -> None:
    fake = FakePassePartout()
    fake.register_error("https://502.example", PassePartoutUnavailable("upstream"))
    with pytest.raises(PassePartoutUnavailable):
        await fake.fetch("https://502.example")


async def test_fake_busy() -> None:
    fake = FakePassePartout()
    fake.register_error("https://busy.example", PassePartoutBusy("max tabs"))
    with pytest.raises(PassePartoutBusy):
        await fake.fetch("https://busy.example")


async def test_fake_auth_error() -> None:
    fake = FakePassePartout()
    fake.register_error("https://auth.example", PassePartoutAuthError("401"))
    with pytest.raises(PassePartoutAuthError):
        await fake.fetch("https://auth.example")


async def test_fake_unsupported_content_type() -> None:
    fake = FakePassePartout()
    fake.register_error("https://pdf.example", UnsupportedContentType("application/pdf"))
    with pytest.raises(UnsupportedContentType) as exc_info:
        await fake.fetch("https://pdf.example")
    assert exc_info.value.content_type == "application/pdf"


async def test_fake_unregistered_url_raises() -> None:
    fake = FakePassePartout()
    with pytest.raises(KeyError):
        await fake.fetch("https://unregistered.example")


async def test_fake_records_calls_in_order() -> None:
    fake = FakePassePartout()
    fake.register("https://a.example", html="a")
    fake.register("https://b.example", html="b")
    await fake.fetch("https://a.example")
    await fake.fetch("https://b.example")
    await fake.fetch("https://a.example")
    assert fake.calls == [
        "https://a.example",
        "https://b.example",
        "https://a.example",
    ]
