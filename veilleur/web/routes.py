"""HTML routes for the Phase 9 operator UI.

All endpoints sit under ``/ui`` so they don't shadow the JSON REST API or
the public RSS/Atom routes that share the ``/feeds/{id}/...`` prefix. The
sole exception is ``GET /`` which renders the feed list (it doesn't
collide with anything else mounted on the app).

Form posts redirect (303 See Other) to the relevant view so the browser's
back/refresh behavior is sane and the UI can be driven without JavaScript.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from veilleur.api.deps import get_llm_client, get_scraper
from veilleur.config import get_settings
from veilleur.db.models import Feed, FeedItem, ScrapeRun
from veilleur.db.session import get_session
from veilleur.feeds.render import MAX_ITEMS
from veilleur.pipeline import run_scrape
from veilleur.scraper import Scraper
from veilleur.web.auth import require_basic_auth
from veilleur.xpath import LLMClient

# `__file__` lives in `veilleur/web/`, so static + templates are siblings.
_PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = _PACKAGE_DIR / "static"
TEMPLATES_DIR = _PACKAGE_DIR / "templates"

# URL path the FastAPI app should mount the static directory at. Kept
# alongside the directory itself so callers don't have to invent it.
STATIC_PATH = "/static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%d %H:%M")
    return value.strftime("%Y-%m-%d %H:%M %Z").strip()


templates.env.filters["datetime"] = _format_datetime


def _static_url(path: str) -> str:
    """Stable static URL helper exposed to templates.

    Avoids relying on ``url_for("static")`` which depends on the mount
    being attached to the same app as the router — handy for tests that
    construct a bare ``TestClient`` without the static mount.
    """
    if not path.startswith("/"):
        path = "/" + path
    return path


# Bind the helper into the Jinja env so templates can call it directly.
# The Jinja2 globals dict is typed as accepting only its built-in callables;
# silence the false positive for our extra helper.
templates.env.globals["static_url"] = _static_url  # type: ignore[assignment]


web_router = APIRouter(
    tags=["web-ui"],
    dependencies=[Depends(require_basic_auth)],
    default_response_class=HTMLResponse,
)


# --- Helpers -----------------------------------------------------------------


async def _get_feed_or_404(session: AsyncSession, feed_id: uuid.UUID) -> Feed:
    stmt = (
        select(Feed)
        .where(Feed.id == feed_id)
        .options(selectinload(Feed.active_xpath_extractor))
    )
    feed = (await session.execute(stmt)).scalar_one_or_none()
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    return feed


def _redirect(target: str) -> RedirectResponse:
    """303 See Other so POST -> GET hand-offs work in browsers."""
    return RedirectResponse(url=target, status_code=status.HTTP_303_SEE_OTHER)


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> HTMLResponse:
    payload: dict[str, Any] = {"request": request, **context}
    payload.setdefault("flash", None)
    return templates.TemplateResponse(
        request=request,
        name=template,
        context=payload,
        status_code=status_code,
    )


# --- Read views --------------------------------------------------------------


@web_router.get("/", name="index")
async def index(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    feeds_stmt = (
        select(Feed)
        .options(selectinload(Feed.active_xpath_extractor))
        .order_by(Feed.created_at.desc(), Feed.id.desc())
    )
    feeds = list((await session.execute(feeds_stmt)).scalars().all())

    counts: dict[uuid.UUID, int] = {}
    if feeds:
        counts_stmt = (
            select(FeedItem.feed_id, func.count(FeedItem.id))
            .where(FeedItem.feed_id.in_([f.id for f in feeds]))
            .group_by(FeedItem.feed_id)
        )
        counts = {fid: cnt for fid, cnt in (await session.execute(counts_stmt)).all()}

    rows = [{"feed": feed, "item_count": counts.get(feed.id, 0)} for feed in feeds]
    return _render(request, "index.html", {"feeds": rows})


@web_router.get("/ui/feeds/{feed_id}", name="feed_detail")
async def feed_detail(
    feed_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    feed = await _get_feed_or_404(session, feed_id)
    items_stmt = (
        select(FeedItem)
        .where(FeedItem.feed_id == feed_id)
        .order_by(FeedItem.first_seen_at.desc(), FeedItem.id.desc())
        .limit(MAX_ITEMS)
    )
    items = list((await session.execute(items_stmt)).scalars().all())
    runs_stmt = (
        select(ScrapeRun)
        .where(ScrapeRun.feed_id == feed_id)
        .order_by(ScrapeRun.started_at.desc())
        .limit(20)
    )
    runs = list((await session.execute(runs_stmt)).scalars().all())
    active_xpath = (
        feed.active_xpath_extractor.xpath
        if feed.active_xpath_extractor is not None
        else None
    )
    return _render(
        request,
        "feed_detail.html",
        {
            "feed": feed,
            "items": items,
            "runs": runs,
            "active_xpath": active_xpath,
        },
    )


# --- Form-driven mutations ---------------------------------------------------


@web_router.post("/ui/feeds", name="create_feed_form")
async def create_feed_form(
    request: Request,
    url: str = Form(...),
    title: str | None = Form(default=None),
    poll_interval_seconds: int | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    settings = get_settings()
    title = (title or "").strip() or url
    interval = poll_interval_seconds or settings.SCRAPE_DEFAULT_INTERVAL_SECONDS
    if interval <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="poll_interval_seconds must be positive",
        )
    feed = Feed(url=url, title=title, poll_interval_seconds=interval)
    session.add(feed)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a feed with this URL already exists",
        ) from exc
    return _redirect(str(request.url_for("feed_detail", feed_id=feed.id)))


@web_router.post("/ui/feeds/{feed_id}/edit", name="update_feed_form")
async def update_feed_form(
    feed_id: uuid.UUID,
    request: Request,
    title: str | None = Form(default=None),
    poll_interval_seconds: int | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    feed = await _get_feed_or_404(session, feed_id)
    if title is not None and title.strip():
        feed.title = title.strip()
    if poll_interval_seconds is not None:
        if poll_interval_seconds <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="poll_interval_seconds must be positive",
            )
        feed.poll_interval_seconds = poll_interval_seconds
    feed.updated_at = datetime.now(tz=feed.created_at.tzinfo)
    await session.commit()
    return _redirect(str(request.url_for("feed_detail", feed_id=feed.id)))


@web_router.post("/ui/feeds/{feed_id}/pause", name="pause_feed_form")
async def pause_feed_form(
    feed_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    feed = await _get_feed_or_404(session, feed_id)
    feed.status = "paused"
    feed.updated_at = datetime.now(tz=feed.created_at.tzinfo)
    await session.commit()
    return _redirect(str(request.url_for("index")))


@web_router.post("/ui/feeds/{feed_id}/unpause", name="unpause_feed_form")
async def unpause_feed_form(
    feed_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    feed = await _get_feed_or_404(session, feed_id)
    feed.status = "active"
    feed.last_failure_reason = None
    feed.updated_at = datetime.now(tz=feed.created_at.tzinfo)
    await session.commit()
    return _redirect(str(request.url_for("index")))


@web_router.post("/ui/feeds/{feed_id}/delete", name="delete_feed_form")
async def delete_feed_form(
    feed_id: uuid.UUID,
    request: Request,
    method_override: str | None = Form(default=None, alias="_method"),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if method_override is not None and method_override.upper() != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only _method=DELETE is supported here",
        )
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    await session.delete(feed)
    await session.commit()
    return _redirect(str(request.url_for("index")))


@web_router.post("/ui/feeds/{feed_id}/scrape", name="scrape_feed_form")
async def scrape_feed_form(
    feed_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    scraper: Scraper = Depends(get_scraper),
    llm: LLMClient = Depends(get_llm_client),
) -> Response:
    exists = await session.execute(select(func.count()).where(Feed.id == feed_id))
    if exists.scalar_one() == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    await run_scrape(feed_id, scraper=scraper, llm=llm)
    return _redirect(str(request.url_for("feed_detail", feed_id=feed_id)))
