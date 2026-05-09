"""add xpath_attempts to scrape_runs

Revision ID: b7d4e2f1a3c8
Revises: 02d3fd33a5ed
Create Date: 2026-05-09 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b7d4e2f1a3c8'
down_revision: Union[str, Sequence[str], None] = '02d3fd33a5ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'scrape_runs',
        sa.Column(
            'xpath_attempts',
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scrape_runs', 'xpath_attempts')
