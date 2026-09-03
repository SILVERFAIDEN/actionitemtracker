"""add is_admin to users

Revision ID: 299964689045
Revises: 967b09385e84
Create Date: 2026-07-22 22:52:18.208109

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '299964689045'
down_revision: Union[str, Sequence[str], None] = '967b09385e84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('users', sa.Column('is_admin', sa.Boolean(), nullable=False, server_default='false'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('users', 'is_admin')
