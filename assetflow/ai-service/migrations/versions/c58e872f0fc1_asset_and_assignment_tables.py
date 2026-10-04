"""asset and assignment tables

Revision ID: c58e872f0fc1
Revises: 
Create Date: 2026-09-18 15:52:50.313284

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c58e872f0fc1'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE TYPE user_kind AS ENUM ('employee', 'it_operator')")
    op.execute("CREATE TYPE asset_category AS ENUM ('LAPTOP', 'PERIPHERAL', 'MOBILE')")
    op.execute("CREATE TYPE inspection_status AS ENUM ('pending', 'confirmed_faulty', 'rejected')")

    op.execute("""
        CREATE TABLE app_user (
          id           bigserial PRIMARY KEY,
          kind         user_kind   NOT NULL,
          display_name text        NOT NULL,
          can_approve  boolean     NOT NULL DEFAULT false,
          created_at   timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT employee_cannot_approve
            CHECK (kind = 'it_operator' OR can_approve = false)
        )
    """)

    op.execute("""
        CREATE TABLE asset_model (
          id       bigserial PRIMARY KEY,
          code     text NOT NULL UNIQUE,
          name     text NOT NULL,
          category asset_category NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE asset_stock (
          asset_model_id bigint PRIMARY KEY REFERENCES asset_model(id),
          available_qty  integer NOT NULL,
          CONSTRAINT stock_not_negative CHECK (available_qty >= 0)
        )
    """)

    op.execute("""
        CREATE TABLE assignment (
          id          bigserial PRIMARY KEY,
          employee_id bigint      NOT NULL REFERENCES app_user(id),
          status      text        NOT NULL,
          assigned_at timestamptz NOT NULL
        )
    """)

    op.execute("""
        CREATE TABLE assignment_item (
          id                 bigserial PRIMARY KEY,
          assignment_id      bigint  NOT NULL REFERENCES assignment(id),
          asset_model_id     bigint  NOT NULL REFERENCES asset_model(id),
          qty                integer NOT NULL,
          unit_acquired_cost numeric(12,2) NOT NULL,
          handover_status    text    NOT NULL,
          allocated_qty      integer NOT NULL DEFAULT 0,
          CONSTRAINT qty_positive      CHECK (qty > 0),
          CONSTRAINT cost_not_negative CHECK (unit_acquired_cost >= 0),
          CONSTRAINT allocation_within_qty
            CHECK (allocated_qty >= 0 AND allocated_qty <= qty)
        )
    """)
    # assignment_item.handover_status:  이 할당 항목의 인계/지급 진행 상태
    # allocated_qty >= 0 AND allocated_qty <= qty:  허가 수량은 요청 수량을 초과할 수 없다
    
    # inspection.status: 이 할당 항목에 대해 실시한 검수 판정 상태
    op.execute("""
        CREATE TABLE inspection (
          assignment_item_id bigint PRIMARY KEY REFERENCES assignment_item(id),
          status             inspection_status NOT NULL DEFAULT 'pending',
          inspected_by       bigint REFERENCES app_user(id),
          inspected_at       timestamptz,
          CONSTRAINT confirmed_requires_inspector
            CHECK (status = 'pending'
                   OR (inspected_by IS NOT NULL AND inspected_at IS NOT NULL))
        )
    """)


def downgrade() -> None:
    for table in ("inspection", "assignment_item", "assignment",
                  "asset_stock", "asset_model", "app_user"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    for enum_type in ("inspection_status", "asset_category", "user_kind"):
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")
