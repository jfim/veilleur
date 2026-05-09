"""Feed CRUD, item history, and scrape-run endpoints.

All routes require a valid bearer token (see :mod:`veilleur.api.auth`).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import and_, func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from veilleur.api.auth import require_bearer_token
from veilleur.api.deps import get_llm_client, get_scraper
from veilleur.api.schemas import (
    FeedCreate,
    FeedItemListResponse,
    FeedItemRead,
    FeedListResponse,
    FeedRead,
    FeedUpdate,
    ScrapeRunListResponse,
    ScrapeRunRead,
    ScrapeTriggerResponse,
    decode_cursor,
    encode_cursor,
)
from veilleur.config import get_settings
from veilleur.db.models import Feed, FeedItem, ScrapeRun
from veilleur.db.session import get_session
from veilleur.pipeline import run_scrape
from veilleur.scraper import Scraper
from veilleur.xpath import LLMClient

router = APIRouter(
    prefix="/feeds",
    tags=["feeds"],
    dependencies=[Depends(require_bearer_token)],
)

# --- Helpers -----------------------------------------------------------------


def _feed_to_read(feed: Feed) -> FeedRead:
    active_xpath = (
        feed.active_xpath_extractor.xpath
        if feed.active_xpath_extractor is not None
        else None
    )
    return FeedRead(
        id=feed.id,
        url=feed.url,
        title=feed.title,
        status=feed.status,  # type: ignore[arg-type]
        poll_interval_seconds=feed.poll_interval_seconds,
        active_xpath=active_xpath,
        last_scraped_at=feed.last_scraped_at,
        last_failure_reason=feed.last_failure_reason,
        created_at=feed.created_at,
        updated_at=feed.updated_at,
    )


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


# --- Routes ------------------------------------------------------------------


@router.post(
    "",
    response_model=FeedRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_feed(
    body: FeedCreate,
    session: AsyncSession = Depends(get_session),
) -> FeedRead:
    settings = get_settings()
    url = str(body.url)
    feed = Feed(
        url=url,
        title=body.title or url,
        poll_interval_seconds=body.poll_interval_seconds
        or settings.SCRAPE_DEFAULT_INTERVAL_SECONDS,
    )
    session.add(feed)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a feed with this URL already exists",
        ) from exc
    await session.refresh(feed, attribute_names=["active_xpath_extractor"])
    return _feed_to_read(feed)


@router.get("", response_model=FeedListResponse)
async def list_feeds(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
) -> FeedListResponse:
    stmt = (
        select(Feed)
        .options(selectinload(Feed.active_xpath_extractor))
        .order_by(Feed.created_at.desc(), Feed.id.desc())
        .limit(limit + 1)
    )
    if cursor:
        try:
            cur_ts, cur_id = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid cursor: {exc}",
            ) from exc
        stmt = stmt.where(
            or_(
                Feed.created_at < cur_ts,
                and_(Feed.created_at == cur_ts, Feed.id < cur_id),
            )
        )
    feeds = list((await session.execute(stmt)).scalars().all())
    next_cursor: str | None = None
    if len(feeds) > limit:
        last = feeds[limit - 1]
        feeds = feeds[:limit]
        next_cursor = encode_cursor(last.created_at, last.id)
    return FeedListResponse(
        items=[_feed_to_read(f) for f in feeds],
        next_cursor=next_cursor,
    )


@router.get("/{feed_id}", response_model=FeedRead)
async def get_feed(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> FeedRead:
    feed = await _get_feed_or_404(session, feed_id)
    return _feed_to_read(feed)


@router.patch("/{feed_id}", response_model=FeedRead)
async def update_feed(
    feed_id: uuid.UUID,
    body: FeedUpdate,
    session: AsyncSession = Depends(get_session),
) -> FeedRead:
    feed = await _get_feed_or_404(session, feed_id)
    if body.title is not None:
        feed.title = body.title
    if body.poll_interval_seconds is not None:
        feed.poll_interval_seconds = body.poll_interval_seconds
    if body.status is not None:
        feed.status = body.status
        if body.status == "active":
            # Resuming a paused/failed feed: clear the stale failure reason.
            feed.last_failure_reason = None
    feed.updated_at = datetime.now(tz=feed.created_at.tzinfo)
    await session.commit()
    await session.refresh(feed, attribute_names=["active_xpath_extractor"])
    return _feed_to_read(feed)


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_feed(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    await session.delete(feed)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{feed_id}/scrape",
    response_model=ScrapeTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_scrape(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    scraper: Scraper = Depends(get_scraper),
    llm: LLMClient = Depends(get_llm_client),
) -> ScrapeTriggerResponse:
    # Validate the feed exists before kicking off the pipeline so that bad
    # IDs return 404 instead of an opaque ValueError.
    exists = await session.execute(select(func.count()).where(Feed.id == feed_id))
    if exists.scalar_one() == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")

    outcome = await run_scrape(feed_id, scraper=scraper, llm=llm)
    return ScrapeTriggerResponse(
        scrape_run_id=outcome.scrape_run_id,
        status=outcome.status,
        items_seen=outcome.items_seen,
        items_new=outcome.items_new,
        error_message=outcome.error_message,
        skipped=outcome.skipped,
    )


@router.get("/{feed_id}/items", response_model=FeedItemListResponse)
async def list_feed_items(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    since: datetime | None = Query(default=None),
) -> FeedItemListResponse:
    # 404 fast for unknown feeds rather than silently returning [].
    exists = await session.execute(select(func.count()).where(Feed.id == feed_id))
    if exists.scalar_one() == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")

    stmt = (
        select(FeedItem)
        .where(FeedItem.feed_id == feed_id)
        .order_by(FeedItem.first_seen_at.desc(), FeedItem.id.desc())
        .limit(limit + 1)
    )
    if since is not None:
        stmt = stmt.where(FeedItem.first_seen_at >= since)
    if cursor:
        try:
            cur_ts, cur_id = decode_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid cursor: {exc}",
            ) from exc
        stmt = stmt.where(
            tuple_(FeedItem.first_seen_at, FeedItem.id) < (cur_ts, cur_id)
        )

    rows = list((await session.execute(stmt)).scalars().all())
    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        rows = rows[:limit]
        next_cursor = encode_cursor(last.first_seen_at, last.id)

    return FeedItemListResponse(
        items=[FeedItemRead.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get("/{feed_id}/runs", response_model=ScrapeRunListResponse)
async def list_scrape_runs(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=50, ge=1, le=500),
) -> ScrapeRunListResponse:
    exists = await session.execute(select(func.count()).where(Feed.id == feed_id))
    if exists.scalar_one() == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    stmt = (
        select(ScrapeRun)
        .where(ScrapeRun.feed_id == feed_id)
        .order_by(ScrapeRun.started_at.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return ScrapeRunListResponse(items=[ScrapeRunRead.model_validate(r) for r in rows])
