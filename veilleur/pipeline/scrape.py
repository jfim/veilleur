"""End-to-end scrape pipeline.

`run_scrape(feed_id)` performs one full scrape:

1. Fetch the feed's URL via passe-partout.
2. Extract anchors from the resulting HTML.
3. If the feed has an active xpath extractor, apply it; diff against the
   previous successful scrape's items via :mod:`veilleur.xpath.validation`.
4. If the diff yields ``Regenerate`` (or there is no active extractor),
   ask the LLM for a fresh xpath, persist it, and re-apply.
5. Persist new items (skip duplicates by ``(feed_id, guid)``; touch
   ``last_seen_at`` on existing rows). Finalize the ``scrape_runs`` row
   and update the feed's ``status`` / ``last_failure_reason`` /
   ``last_scraped_at``.

Concurrency: a process-wide :class:`asyncio.Lock` serializes only the
passe-partout interaction (``scraper.fetch``) so we never open more than
one tab at a time. The LLM call, parsing, validation, and DB writes run
outside the lock so a slow ``derive_xpath`` on one feed doesn't block
other feeds from starting their fetch.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from veilleur.db.models import Feed, FeedItem, ScrapeRun, XPathExtractor
from veilleur.db.session import get_session_factory
from veilleur.scraper import (
    FetchError,
    FetchResult,
    Scraper,
    UnsupportedContentType,
    write_html,
)
from veilleur.xpath import (
    AnchorExtractionError,
    AnchorExtractionResult,
    Item,
    LLMClient,
    XPathAttempt,
    XPathDerivationFailed,
    XPathToolkitError,
    apply_xpath,
    derive_xpath,
    extract_anchors,
)
from veilleur.xpath.validation import (
    Fail,
    FirstRun,
    Ok,
    Regenerate,
    validate,
)

logger = logging.getLogger(__name__)

_FETCH_LOCK = asyncio.Lock()


async def _locked_fetch(scraper: Scraper, url: str) -> FetchResult:
    """Acquire the global passe-partout lock for the duration of one fetch."""
    async with _FETCH_LOCK:
        return await scraper.fetch(url)


@dataclass(frozen=True, slots=True)
class ScrapeOutcome:
    """Result of one ``run_scrape`` call."""

    scrape_run_id: uuid.UUID
    status: str
    items_seen: int
    items_new: int
    error_message: str | None
    skipped: bool = False


async def run_scrape(
    feed_id: uuid.UUID,
    *,
    scraper: Scraper,
    llm: LLMClient,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    force_regenerate: bool = False,
) -> ScrapeOutcome:
    """Run a single scrape for ``feed_id``.

    Only the passe-partout fetch is serialized via a process-wide lock; the
    LLM call, parsing, validation, and DB writes run without the lock.

    ``force_regenerate=True`` bypasses any active xpath and asks the LLM
    for a fresh expression. The previous-links validation step is skipped
    (the user is explicitly asking for new behavior, so we can't expect
    the new xpath to overlap with the old one).
    """
    factory = session_factory or get_session_factory()
    return await _run(feed_id, scraper, llm, factory, force_regenerate)


async def _run(
    feed_id: uuid.UUID,
    scraper: Scraper,
    llm: LLMClient,
    factory: async_sessionmaker[AsyncSession],
    force_regenerate: bool,
) -> ScrapeOutcome:
    # --- 1. Load feed + open scrape_run --------------------------------------
    async with factory() as session:
        feed = await _load_feed(session, feed_id)
        if feed is None:
            raise ValueError(f"feed {feed_id} not found")
        if feed.status == "paused":
            return ScrapeOutcome(
                scrape_run_id=uuid.UUID(int=0),
                status="skipped",
                items_seen=0,
                items_new=0,
                error_message=None,
                skipped=True,
            )
        run = ScrapeRun(
            feed_id=feed.id,
            xpath_extractor_id=feed.active_xpath_extractor_id,
            status="running",
        )
        session.add(run)
        await session.commit()
        run_id = run.id
        feed_url = feed.url
        active_xpath = (
            feed.active_xpath_extractor.xpath
            if feed.active_xpath_extractor is not None
            else None
        )

    # --- 2. Fetch (single-flight via _FETCH_LOCK) ----------------------------
    try:
        fetch = await _locked_fetch(scraper, feed_url)
    except UnsupportedContentType as exc:
        return await _finalize_failure(
            factory, run_id, feed_id, str(exc), http_status=None, raw_html=None
        )
    except FetchError as exc:
        return await _finalize_failure(
            factory, run_id, feed_id, str(exc), http_status=None, raw_html=None
        )


    # --- 3. Extract anchors --------------------------------------------------
    try:
        anchors = extract_anchors(fetch.html, fetch.final_url)
    except AnchorExtractionError as exc:
        return await _finalize_failure(
            factory,
            run_id,
            feed_id,
            f"anchor extraction: {exc}",
            http_status=fetch.status_code,
            raw_html=fetch.html,
        )

    # --- 4 + 5. Decide path: existing-xpath or derive-fresh ------------------
    items: list[Item]
    new_extractor_id: uuid.UUID | None = None
    final_status = "success"
    derive_attempts_serialized: list[dict[str, object]] | None = None

    prev_links: list[str] | None = await _load_prev_links(factory, feed_id)

    if force_regenerate:
        # Operator explicitly asked for a fresh xpath. Bypass the active
        # xpath entirely and skip prev-links validation — the new xpath
        # may legitimately match a different set of items than the old one.
        return await _try_regenerate_or_fail(
            factory=factory,
            run_id=run_id,
            feed_id=feed_id,
            fetch=fetch,
            anchors=anchors,
            llm=llm,
            prev_links=None,
            reason="forced regeneration",
            strict=False,
        )

    if active_xpath is not None:
        try:
            items = apply_xpath(fetch.html, fetch.final_url, active_xpath)
        except XPathToolkitError as exc:
            # Existing xpath broke entirely — regenerate.
            return await _try_regenerate_or_fail(
                factory=factory,
                run_id=run_id,
                feed_id=feed_id,
                fetch=fetch,
                anchors=anchors,
                llm=llm,
                prev_links=prev_links,
                reason=f"existing xpath unusable: {exc}",
                strict=prev_links is not None,
            )
        decision = validate(prev_links, [it.url for it in items])
        match decision:
            case Ok() | FirstRun():
                pass  # keep items, success
            case Regenerate():
                return await _try_regenerate_or_fail(
                    factory=factory,
                    run_id=run_id,
                    feed_id=feed_id,
                    fetch=fetch,
                    anchors=anchors,
                    llm=llm,
                    prev_links=prev_links,
                    reason=decision.reason,
                    strict=True,
                )
            case Fail():
                return await _finalize_failure(
                    factory,
                    run_id,
                    feed_id,
                    f"validation failed: {decision.reason}",
                    http_status=fetch.status_code,
                    raw_html=fetch.html,
                )
    else:
        # No active extractor — derive one.
        try:
            outcome = await derive_xpath(
                anchors.title,
                fetch.final_url,
                anchors.anchors,
                llm,
                root=anchors.root,  # type: ignore[arg-type]
                elements=anchors.elements,
            )
        except XPathDerivationFailed as exc:
            return await _finalize_failure(
                factory,
                run_id,
                feed_id,
                f"xpath derivation failed: {exc}",
                http_status=fetch.status_code,
                raw_html=fetch.html,
                xpath_attempts=None,
            )
        new_xpath = outcome.xpath
        derive_attempts: tuple[XPathAttempt, ...] = outcome.attempts
        try:
            items = apply_xpath(fetch.html, fetch.final_url, new_xpath)
        except XPathToolkitError as exc:
            return await _finalize_failure(
                factory,
                run_id,
                feed_id,
                f"derived xpath unusable: {exc}",
                http_status=fetch.status_code,
                raw_html=fetch.html,
                xpath_attempts=_serialize_attempts(derive_attempts),
            )
        # No prev_links to validate against on first run; accept items.
        new_extractor_id = await _persist_new_extractor(
            factory, feed_id, new_xpath, _resolve_llm_model()
        )
        final_status = "xpath_regenerated"
        derive_attempts_serialized = _serialize_attempts(derive_attempts)

    # --- 6 + 7. Persist items + finalize -------------------------------------
    items_seen, items_new = await _persist_items(factory, feed_id, items, run_id)
    await _finalize_success(
        factory=factory,
        run_id=run_id,
        feed_id=feed_id,
        status=final_status,
        items_seen=items_seen,
        items_new=items_new,
        http_status=fetch.status_code,
        raw_html=fetch.html,
        new_extractor_id=new_extractor_id,
        xpath_attempts=derive_attempts_serialized,
    )
    return ScrapeOutcome(
        scrape_run_id=run_id,
        status=final_status,
        items_seen=items_seen,
        items_new=items_new,
        error_message=None,
    )


async def _try_regenerate_or_fail(
    *,
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    feed_id: uuid.UUID,
    fetch: FetchResult,
    anchors: AnchorExtractionResult,
    llm: LLMClient,
    prev_links: list[str] | None,
    reason: str,
    strict: bool,
) -> ScrapeOutcome:
    """One regeneration attempt; on any failure mark the feed failed."""
    attempts_serialized: list[dict[str, object]] | None = None
    try:
        outcome = await derive_xpath(
            anchors.title,
            fetch.final_url,
            anchors.anchors,
            llm,
            root=anchors.root,  # type: ignore[arg-type]
            elements=anchors.elements,
        )
    except XPathDerivationFailed as exc:
        return await _finalize_failure(
            factory,
            run_id,
            feed_id,
            f"regenerate failed ({reason}): {exc}",
            http_status=fetch.status_code,
            raw_html=fetch.html,
            xpath_attempts=None,
        )
    except XPathToolkitError as exc:
        # Catches LLMClientError and any other toolkit-level failure
        # (transport, auth, malformed reply) so the scrape_run row is
        # finalized as failed rather than left orphaned at "running".
        return await _finalize_failure(
            factory,
            run_id,
            feed_id,
            f"regenerate failed ({reason}): {exc}",
            http_status=fetch.status_code,
            raw_html=fetch.html,
            xpath_attempts=None,
        )
    new_xpath = outcome.xpath
    attempts_serialized = _serialize_attempts(outcome.attempts)
    try:
        items = apply_xpath(fetch.html, fetch.final_url, new_xpath)
    except XPathToolkitError as exc:
        return await _finalize_failure(
            factory,
            run_id,
            feed_id,
            f"regenerated xpath unusable: {exc}",
            http_status=fetch.status_code,
            raw_html=fetch.html,
            xpath_attempts=attempts_serialized,
        )

    if strict and prev_links is not None:
        re_decision = validate(prev_links, [it.url for it in items])
        if not isinstance(re_decision, (Ok, FirstRun)):
            reason_text = re_decision.reason
            return await _finalize_failure(
                factory,
                run_id,
                feed_id,
                f"regenerated xpath still does not match: {reason_text}",
                http_status=fetch.status_code,
                raw_html=fetch.html,
                xpath_attempts=attempts_serialized,
            )

    new_extractor_id = await _persist_new_extractor(
        factory, feed_id, new_xpath, _resolve_llm_model()
    )
    items_seen, items_new = await _persist_items(factory, feed_id, items, run_id)
    await _finalize_success(
        factory=factory,
        run_id=run_id,
        feed_id=feed_id,
        status="xpath_regenerated",
        items_seen=items_seen,
        items_new=items_new,
        http_status=fetch.status_code,
        raw_html=fetch.html,
        new_extractor_id=new_extractor_id,
        xpath_attempts=attempts_serialized,
    )
    return ScrapeOutcome(
        scrape_run_id=run_id,
        status="xpath_regenerated",
        items_seen=items_seen,
        items_new=items_new,
        error_message=None,
    )


# --- Helpers -----------------------------------------------------------------


async def _load_feed(session: AsyncSession, feed_id: uuid.UUID) -> Feed | None:
    stmt = (
        select(Feed)
        .where(Feed.id == feed_id)
        .options(selectinload(Feed.active_xpath_extractor))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _load_prev_links(
    factory: async_sessionmaker[AsyncSession], feed_id: uuid.UUID
) -> list[str] | None:
    """Return the URLs from the most recent successful scrape, or None."""
    async with factory() as session:
        stmt = (
            select(ScrapeRun.id)
            .where(
                ScrapeRun.feed_id == feed_id,
                ScrapeRun.status.in_(["success", "xpath_regenerated"]),
            )
            .order_by(ScrapeRun.started_at.desc())
            .limit(1)
        )
        last_run_id = (await session.execute(stmt)).scalar_one_or_none()
        if last_run_id is None:
            return None
        items_stmt = (
            select(FeedItem.url)
            .where(FeedItem.scrape_run_id == last_run_id)
            .order_by(FeedItem.first_seen_at.asc())
        )
        return list((await session.execute(items_stmt)).scalars().all())


async def _persist_new_extractor(
    factory: async_sessionmaker[AsyncSession],
    feed_id: uuid.UUID,
    xpath: str,
    model: str | None,
) -> uuid.UUID:
    async with factory() as session:
        extractor = XPathExtractor(
            feed_id=feed_id,
            xpath=xpath,
            generated_by="llm",
            llm_model=model,
        )
        session.add(extractor)
        await session.flush()
        feed = await session.get(Feed, feed_id)
        assert feed is not None
        feed.active_xpath_extractor_id = extractor.id
        await session.commit()
        return extractor.id


async def _persist_items(
    factory: async_sessionmaker[AsyncSession],
    feed_id: uuid.UUID,
    items: list[Item],
    scrape_run_id: uuid.UUID,
) -> tuple[int, int]:
    """Insert new items, update last_seen_at on duplicates. Returns (seen, new)."""
    if not items:
        return (0, 0)
    now = datetime.now(UTC)
    rows = [
        {
            "feed_id": feed_id,
            "guid": it.url,
            "url": it.url,
            "title": it.title,
            "first_seen_at": now,
            "last_seen_at": now,
            "scrape_run_id": scrape_run_id,
        }
        for it in items
    ]
    async with factory() as session:
        stmt = pg_insert(FeedItem).values(rows)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_feed_items_feed_id_guid",
            set_={"last_seen_at": now},
        ).returning(FeedItem.id, FeedItem.first_seen_at)
        result = await session.execute(stmt)
        # rows where first_seen_at == now means a freshly inserted item
        rows_returned = list(result.all())
        new_count = sum(1 for _, fs in rows_returned if fs == now)
        await session.commit()
        return (len(items), new_count)


async def _finalize_success(
    *,
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    feed_id: uuid.UUID,
    status: str,
    items_seen: int,
    items_new: int,
    http_status: int | None,
    raw_html: str | None,
    new_extractor_id: uuid.UUID | None,
    xpath_attempts: list[dict[str, object]] | None = None,
) -> None:
    finished_at = datetime.now(UTC)
    raw_html_path = _persist_raw_html(
        feed_id=feed_id, run_id=run_id, when=finished_at, raw_html=raw_html
    )
    async with factory() as session:
        run = await session.get(ScrapeRun, run_id)
        assert run is not None
        run.status = status
        run.finished_at = finished_at
        run.items_seen = items_seen
        run.items_new = items_new
        run.http_status = http_status
        if raw_html_path is not None:
            run.raw_html_path = raw_html_path
        if new_extractor_id is not None:
            run.xpath_extractor_id = new_extractor_id
        if xpath_attempts is not None:
            run.xpath_attempts = xpath_attempts

        feed = await session.get(Feed, feed_id)
        assert feed is not None
        feed.status = "active"
        feed.last_failure_reason = None
        feed.last_scraped_at = run.finished_at
        await session.commit()


async def _finalize_failure(
    factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
    feed_id: uuid.UUID,
    error_message: str,
    *,
    http_status: int | None,
    raw_html: str | None,
    xpath_attempts: list[dict[str, object]] | None = None,
) -> ScrapeOutcome:
    logger.warning("scrape %s for feed %s failed: %s", run_id, feed_id, error_message)
    finished_at = datetime.now(UTC)
    raw_html_path = _persist_raw_html(
        feed_id=feed_id, run_id=run_id, when=finished_at, raw_html=raw_html
    )
    async with factory() as session:
        run = await session.get(ScrapeRun, run_id)
        assert run is not None
        run.status = "failed"
        run.finished_at = finished_at
        run.error_message = error_message
        run.http_status = http_status
        if raw_html_path is not None:
            run.raw_html_path = raw_html_path
        if xpath_attempts is not None:
            run.xpath_attempts = xpath_attempts

        feed = await session.get(Feed, feed_id)
        assert feed is not None
        feed.status = "failed"
        feed.last_failure_reason = error_message
        feed.last_scraped_at = run.finished_at
        await session.commit()
    return ScrapeOutcome(
        scrape_run_id=run_id,
        status="failed",
        items_seen=0,
        items_new=0,
        error_message=error_message,
    )


def _persist_raw_html(
    *,
    feed_id: uuid.UUID,
    run_id: uuid.UUID,
    when: datetime,
    raw_html: str | None,
) -> str | None:
    """Write ``raw_html`` to disk if storage is configured.

    Returns the relative path to record on the scrape_run row, or ``None``
    when there is nothing to persist (no html, or no configured storage).
    Disk-write failures are logged and swallowed: a successful scrape must
    not be downgraded to a failure because a debug artifact could not be
    written.
    """
    if raw_html is None:
        return None
    from veilleur.config import get_settings

    raw_html_dir = get_settings().VEILLEUR_RAW_HTML_DIR
    if raw_html_dir is None:
        return None
    try:
        return write_html(
            raw_html_dir=raw_html_dir,
            feed_id=feed_id,
            run_id=run_id,
            when=when,
            html=raw_html,
        )
    except OSError:
        logger.exception(
            "failed to persist raw HTML for run %s under %s",
            run_id,
            raw_html_dir,
        )
        return None


def _serialize_attempts(
    attempts: tuple[XPathAttempt, ...],
) -> list[dict[str, object]] | None:
    """Render xpath-derivation attempts as JSON-serializable dicts.

    Returns ``None`` for empty traces so the column stays NULL when the
    loop didn't run (rather than persisting an empty array).
    """
    if not attempts:
        return None
    return [
        {
            "turn": a.turn,
            "xpath": a.xpath,
            "intended_ids": list(a.intended_ids),
            "actual_ids": list(a.actual_ids),
            "extras_outside_listing": a.extras_outside_listing,
            "parse_error": a.parse_error,
            "unable": a.unable,
            "ok": a.ok,
        }
        for a in attempts
    ]


def _resolve_llm_model() -> str:
    from veilleur.config import get_settings

    settings = get_settings()
    return settings.LLM_MODEL_NAME or ""
