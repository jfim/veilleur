"""add raw_html_path to scrape_runs

Revision ID: a3f1c8b9d201
Revises: dc8b402c1726
Create Date: 2026-05-09 04:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f1c8b9d201'
down_revision: Union[str, Sequence[str], None] = 'dc8b402c1726'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'scrape_runs',
        sa.Column('raw_html_path', sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scrape_runs', 'raw_html_path')
