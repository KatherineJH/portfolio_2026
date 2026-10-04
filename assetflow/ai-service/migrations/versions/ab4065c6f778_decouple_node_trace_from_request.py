"""decouple node_trace from request

Revision ID: ab4065c6f778
Revises: 36af9306fc29
Create Date: 2026-09-26 20:01:05.749724

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ab4065c6f778'
down_revision: Union[str, Sequence[str], None] = '36af9306fc29'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 관측은 업무 트랜잭션과 분리해 기록한다. 별도 커넥션에서 쓰므로
    # 아직 커밋되지 않은 request 를 참조할 수 없다. 외래키를 빼고
    # 느슨한 참조로 둔다 (SYS-18).
    op.execute("ALTER TABLE node_trace DROP CONSTRAINT node_trace_request_id_fkey")
    op.execute("CREATE INDEX node_trace_request_idx ON node_trace (request_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS node_trace_request_idx")
    op.execute(
        "ALTER TABLE node_trace ADD CONSTRAINT node_trace_request_id_fkey "
        "FOREIGN KEY (request_id) REFERENCES request(id)"
    )
