"""store intake response

Revision ID: a7605d3eaab8
Revises: d9a42542c4ff
Create Date: 2026-09-28 20:25:43.375984

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7605d3eaab8'
down_revision: Union[str, Sequence[str], None] = 'd9a42542c4ff'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE request ADD COLUMN intake_response jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE request DROP COLUMN intake_response")