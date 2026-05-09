"""Public RSS / Atom serving endpoints.

These routes are intentionally **unauthenticated** — feed URLs are designed
to be pasted into off-the-shelf feed readers. They sit on the same
``/feeds/{id}/...`` prefix as the bearer-protected REST API, but are
registered as a separate router so the auth dependency does not apply.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from veilleur.db.models import Feed, FeedItem
from veilleur.db.session import get_session
from veilleur.feeds.render import MAX_ITEMS, render_atom, render_rss

router = APIRouter(prefix="/feeds", tags=["feeds-public"])

CACHE_CONTROL = "public, max-age=300"


async def _load_feed_and_items(
    session: AsyncSession, feed_id: uuid.UUID
) -> tuple[Feed, list[FeedItem]]:
    feed = await session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    stmt = (
        select(FeedItem)
        .where(FeedItem.feed_id == feed_id)
        .order_by(FeedItem.first_seen_at.desc(), FeedItem.id.desc())
        .limit(MAX_ITEMS)
    )
    items = list((await session.execute(stmt)).scalars().all())
    return feed, items


@router.get("/{feed_id}/rss")
async def get_rss(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    feed, items = await _load_feed_and_items(session, feed_id)
    body = render_rss(feed, items)
    return Response(
        content=body,
        media_type="application/rss+xml",
        headers={"Cache-Control": CACHE_CONTROL},
    )


@router.get("/{feed_id}/atom")
async def get_atom(
    feed_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    feed, items = await _load_feed_and_items(session, feed_id)
    body = render_atom(feed, items)
    return Response(
        content=body,
        media_type="application/atom+xml",
        headers={"Cache-Control": CACHE_CONTROL},
    )
