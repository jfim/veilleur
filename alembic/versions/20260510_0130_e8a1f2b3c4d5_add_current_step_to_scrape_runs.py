"""add current_step to scrape_runs

Revision ID: e8a1f2b3c4d5
Revises: b7d4e2f1a3c8
Create Date: 2026-05-10 01:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8a1f2b3c4d5'
down_revision: Union[str, Sequence[str], None] = 'b7d4e2f1a3c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'scrape_runs',
        sa.Column('current_step', sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scrape_runs', 'current_step')
