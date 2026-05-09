"""UUIDv7 helper for primary keys.

Phase 1 generates IDs app-side via ``uuid_utils.uuid7`` because Postgres 16
has no native UUIDv7 generator. See ``docs/design/phase-1-foundations.md`` §4.
"""

from __future__ import annotations

import uuid

import uuid_utils


def new_id() -> uuid.UUID:
    """Return a fresh UUIDv7 as a stdlib ``uuid.UUID``."""
    # uuid_utils.uuid7() returns a uuid_utils.UUID; coerce to stdlib UUID for
    # SQLAlchemy / pydantic compatibility.
    return uuid.UUID(str(uuid_utils.uuid7()))
