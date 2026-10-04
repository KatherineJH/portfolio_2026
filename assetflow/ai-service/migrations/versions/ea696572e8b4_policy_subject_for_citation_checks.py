"""policy subject for citation checks

Revision ID: ea696572e8b4
Revises: a7605d3eaab8
Create Date: 2026-09-29 17:02:26.003296

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ea696572e8b4'
down_revision: Union[str, Sequence[str], None] = 'a7605d3eaab8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE policy_subject AS ENUM "
        "('inspection', 'quantity', 'stock', 'ownership', 'liability', 'replacement')"
    )

    # 1) 널 허용으로 추가한다.
    op.execute("ALTER TABLE policy ADD COLUMN subject policy_subject")

    # 2) 기존 행을 채운다.
    op.execute("""
        UPDATE policy SET subject = CASE policy_key
            WHEN 'INSPECTION-REQUIRED' THEN 'inspection'
            WHEN 'REPLACE-QTY'         THEN 'quantity'
            WHEN 'REPLACE-STOCK'       THEN 'stock'
            WHEN 'OWNERSHIP'           THEN 'ownership'
            WHEN 'COST-LIABILITY'      THEN 'liability'
            ELSE 'replacement'
        END::policy_subject
    """)

    # 3) 채운 뒤에야 NOT NULL 을 건다.
    op.execute("ALTER TABLE policy ALTER COLUMN subject SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE policy DROP COLUMN subject")
    op.execute("DROP TYPE policy_subject")
