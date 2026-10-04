"""intake key on request

Revision ID: d9a42542c4ff
Revises: ab4065c6f778
Create Date: 2026-09-28 20:09:00.753298

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9a42542c4ff'
down_revision: Union[str, Sequence[str], None] = 'ab4065c6f778'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE request ADD COLUMN intake_key text")
    op.execute(
        "ALTER TABLE request ADD CONSTRAINT intake_key_length "
        "CHECK (intake_key IS NULL OR length(intake_key) BETWEEN 8 AND 128)"
    )
    op.execute("CREATE UNIQUE INDEX one_request_per_intake_key ON request (intake_key)")


def downgrade() -> None:
    op.execute("DROP INDEX one_request_per_intake_key")
    op.execute("ALTER TABLE request DROP CONSTRAINT intake_key_length")
    op.execute("ALTER TABLE request DROP COLUMN intake_key")