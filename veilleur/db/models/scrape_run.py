"""ScrapeRun ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from veilleur.db.base import Base
from veilleur.db.ids import new_id

if TYPE_CHECKING:
    from veilleur.db.models.feed import Feed
    from veilleur.db.models.xpath_extractor import XPathExtractor


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'success', 'failed', 'xpath_regenerated')",
            name="status_valid",
        ),
        Index(
            "ix_scrape_runs_feed_id_started_at",
            "feed_id",
            text("started_at DESC"),
        ),
        Index("ix_scrape_runs_started_at", "started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    feed_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("feeds.id", ondelete="CASCADE"),
        nullable=False,
    )
    xpath_extractor_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("xpath_extractors.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(nullable=False)
    http_status: Mapped[int | None] = mapped_column(nullable=True)
    raw_html_path: Mapped[str | None] = mapped_column(nullable=True)
    items_seen: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    items_new: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(nullable=True)
    xpath_attempts: Mapped[list[dict[str, object]] | None] = mapped_column(
        JSONB, nullable=True
    )

    feed: Mapped[Feed] = relationship("Feed", back_populates="scrape_runs")
    xpath_extractor: Mapped[XPathExtractor | None] = relationship("XPathExtractor")
