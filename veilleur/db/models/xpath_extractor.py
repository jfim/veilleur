"""XPathExtractor ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import DateTime

from veilleur.db.base import Base
from veilleur.db.ids import new_id

if TYPE_CHECKING:
    from veilleur.db.models.feed import Feed


class XPathExtractor(Base):
    __tablename__ = "xpath_extractors"
    __table_args__ = (
        Index(
            "ix_xpath_extractors_feed_id_created_at",
            "feed_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=new_id
    )
    feed_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("feeds.id", ondelete="CASCADE"),
        nullable=False,
    )
    xpath: Mapped[str] = mapped_column(nullable=False)
    generated_by: Mapped[str] = mapped_column(nullable=False)
    llm_model: Mapped[str | None] = mapped_column(nullable=True)
    notes: Mapped[str | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    feed: Mapped[Feed] = relationship(
        "Feed",
        back_populates="xpath_extractors",
        foreign_keys=[feed_id],
    )
