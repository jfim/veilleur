"""Feed ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from veilleur.db.base import Base
from veilleur.db.ids import new_id

if TYPE_CHECKING:
    from veilleur.db.models.feed_item import FeedItem
    from veilleur.db.models.scrape_run import ScrapeRun
    from veilleur.db.models.xpath_extractor import XPathExtractor


class Feed(Base):
    __tablename__ = "feeds"
    __table_args__ = (
        CheckConstraint("poll_interval_seconds > 0", name="poll_interval_positive"),
        CheckConstraint(
            "status IN ('active', 'paused', 'failed')",
            name="status_valid",
        ),
        Index("ix_feeds_status_last_scraped_at", "status", "last_scraped_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    url: Mapped[str] = mapped_column(unique=True, nullable=False)
    title: Mapped[str] = mapped_column(nullable=False)
    poll_interval_seconds: Mapped[int] = mapped_column(
        nullable=False, server_default=text("3600")
    )
    active_xpath_extractor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "xpath_extractors.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
            name="fk_feeds_active_xpath_extractor_id_xpath_extractors",
        ),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        nullable=False, server_default=text("'active'")
    )
    last_scraped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_failure_reason: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    active_xpath_extractor: Mapped[XPathExtractor | None] = relationship(
        "XPathExtractor",
        foreign_keys=[active_xpath_extractor_id],
        post_update=True,
    )
    items: Mapped[list[FeedItem]] = relationship(
        "FeedItem",
        back_populates="feed",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    scrape_runs: Mapped[list[ScrapeRun]] = relationship(
        "ScrapeRun",
        back_populates="feed",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    xpath_extractors: Mapped[list[XPathExtractor]] = relationship(
        "XPathExtractor",
        back_populates="feed",
        foreign_keys="XPathExtractor.feed_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
