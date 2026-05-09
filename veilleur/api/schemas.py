"""Pydantic request/response models for the REST API."""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

FeedStatus = Literal["active", "paused", "failed"]
EditableFeedStatus = Literal["active", "paused"]


class FeedCreate(BaseModel):
    """Body for ``POST /feeds``."""

    url: AnyHttpUrl
    title: str | None = Field(default=None, min_length=1, max_length=500)
    poll_interval_seconds: int | None = Field(default=None, gt=0)


class FeedUpdate(BaseModel):
    """Body for ``PATCH /feeds/{id}``. All fields optional."""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    poll_interval_seconds: int | None = Field(default=None, gt=0)
    status: EditableFeedStatus | None = None


class FeedRead(BaseModel):
    """Full feed view returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    title: str
    status: FeedStatus
    poll_interval_seconds: int
    active_xpath: str | None
    last_scraped_at: datetime | None
    last_failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class FeedListResponse(BaseModel):
    items: list[FeedRead]
    next_cursor: str | None = None


class FeedItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    feed_id: uuid.UUID
    guid: str
    url: str
    title: str
    summary: str | None
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    scrape_run_id: uuid.UUID | None


class FeedItemListResponse(BaseModel):
    items: list[FeedItemRead]
    next_cursor: str | None = None


class ScrapeRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    feed_id: uuid.UUID
    xpath_extractor_id: uuid.UUID | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    http_status: int | None
    items_seen: int
    items_new: int
    error_message: str | None


class ScrapeRunListResponse(BaseModel):
    items: list[ScrapeRunRead]


class ScrapeTriggerResponse(BaseModel):
    """Response for ``POST /feeds/{id}/scrape``."""

    scrape_run_id: uuid.UUID
    status: str
    items_seen: int
    items_new: int
    error_message: str | None
    skipped: bool


# --- Cursor encoding ---------------------------------------------------------

# Cursors encode an ordering tuple as base64url("<iso_ts>|<uuid>") and are
# opaque to clients. This keeps pagination stable across inserts because we
# order by (first_seen_at desc, id desc) and use the cursor as a strict
# upper-bound filter.

def encode_cursor(ts: datetime, row_id: uuid.UUID) -> str:
    raw = f"{ts.isoformat()}|{row_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid cursor: {exc}") from exc
    if "|" not in raw:
        raise ValueError("invalid cursor: missing separator")
    ts_str, id_str = raw.rsplit("|", 1)
    try:
        ts = datetime.fromisoformat(ts_str)
        row_id = uuid.UUID(id_str)
    except ValueError as exc:
        raise ValueError(f"invalid cursor payload: {exc}") from exc
    return ts, row_id


