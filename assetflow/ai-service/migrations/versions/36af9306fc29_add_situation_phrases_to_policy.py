"""add situation phrases to policy

Revision ID: 36af9306fc29
Revises: 0c7b5d6fb13f
Create Date: 2026-09-22 20:08:00.222267

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36af9306fc29'
down_revision: Union[str, Sequence[str], None] = '0c7b5d6fb13f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE policy ADD COLUMN situation_text text")


def downgrade() -> None:
    op.execute("ALTER TABLE policy DROP COLUMN situation_text")