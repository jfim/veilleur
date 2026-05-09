"""drop scrape_runs raw_html column

Revision ID: 02d3fd33a5ed
Revises: a3f1c8b9d201
Create Date: 2026-05-09 16:25:26.553753

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '02d3fd33a5ed'
down_revision: Union[str, Sequence[str], None] = 'a3f1c8b9d201'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_column('scrape_runs', 'raw_html')


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column(
        'scrape_runs',
        sa.Column('raw_html', sa.Text(), nullable=True),
    )
