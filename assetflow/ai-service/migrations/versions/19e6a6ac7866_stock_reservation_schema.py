"""stock reservation schema (ADR-002, step 1)

Revision ID: 19e6a6ac7866
Revises: ea696572e8b4
Create Date: 2026-10-06

One revision, one PostgreSQL transaction: the pre-check, the rename, the new
table, the approval revocation columns and the index replacement succeed or
fail together. No application logic is part of this revision.

Downgrade is supported only while no reservation and no revoked approval
exists. It refuses otherwise and does not claim to restore data.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '19e6a6ac7866'
down_revision: Union[str, Sequence[str], None] = 'ea696572e8b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 메시지에 담는 요청 id 개수 상한
LISTED = 20


def _ids(rows) -> str:
    ids = [str(r[0]) for r in rows[:LISTED]]
    more = f" (and {len(rows) - LISTED} more)" if len(rows) > LISTED else ""
    return ", ".join(ids) + more


def _precheck() -> None:
    """예약 없이 남아 있는 활성 승인과, 새 인덱스를 만들 수 없는 데이터를 막는다.

    예약을 추측해서 만들지 않는다. 걸리면 마이그레이션이 중단되고 사람이 처리한다.
    """
    bind = op.get_bind()

    pending = bind.execute(sa.text("""
        SELECT DISTINCT a.request_id
        FROM approval a
        JOIN request r ON r.id = a.request_id
        WHERE a.decision = 'approve'
          AND r.state IN ('ready_to_execute', 'outcome_unknown')
          AND NOT EXISTS (
            SELECT 1 FROM request_execution e
            WHERE e.request_id = a.request_id
              AND e.proposal_version = a.proposal_version)
        ORDER BY a.request_id
    """)).all()
    if pending:
        raise RuntimeError(
            "Migration stopped: %d request(s) have an active approval that can "
            "still be executed and no reservation can be created for it: "
            "request id %s. Resolve them explicitly (execute, reject or "
            "withdraw) and run the migration again. No reservation is guessed."
            % (len(pending), _ids(pending)))

    duplicated = bind.execute(sa.text("""
        SELECT request_id FROM approval
        WHERE decision = 'approve'
        GROUP BY request_id HAVING count(*) > 1
        ORDER BY request_id
    """)).all()
    if duplicated:
        raise RuntimeError(
            "Migration stopped: %d request(s) have more than one approval "
            "with decision 'approve', so the one-active-approval-per-request "
            "index cannot be created: request id %s."
            % (len(duplicated), _ids(duplicated)))


def upgrade() -> None:
    _precheck()

    # 컬럼 의미를 바꾼다: 예약이 생기면 "지금 실행 가능한 양"과 "보유량"이 달라진다.
    op.execute("ALTER TABLE asset_stock RENAME COLUMN available_qty TO on_hand_qty")
    op.execute("""
        ALTER TABLE asset_stock
          RENAME CONSTRAINT stock_not_negative TO on_hand_qty_not_negative
    """)

    # 복합 외래키가 참조할 유일 제약. 기존 행에는 영향이 없다.
    op.execute("""
        ALTER TABLE proposal
          ADD CONSTRAINT proposal_id_request_version_key
          UNIQUE (id, request_id, version)
    """)
    op.execute("""
        ALTER TABLE assignment_item
          ADD CONSTRAINT assignment_item_id_model_key
          UNIQUE (id, asset_model_id)
    """)
    op.execute("""
        ALTER TABLE request_execution
          ADD CONSTRAINT request_execution_key_request_version_key
          UNIQUE (execution_key, request_id, proposal_version)
    """)

    # 승인 철회: 승인 기록은 지우지 않고 철회 정보를 덧붙인다.
    op.execute("""
        ALTER TABLE approval
          ADD COLUMN revoked_at   timestamptz,
          ADD COLUMN revoked_by   bigint REFERENCES app_user(id),
          ADD COLUMN revoke_reason text
    """)
    op.execute("""
        ALTER TABLE approval ADD CONSTRAINT approval_revocation_shape CHECK (
          (revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL)
          OR
          (revoked_at IS NOT NULL AND revoked_by IS NOT NULL
           AND revoke_reason IS NOT NULL AND length(btrim(revoke_reason)) > 0))
    """)
    op.execute("""
        ALTER TABLE approval
          ADD CONSTRAINT approval_only_approve_is_revoked
          CHECK (revoked_at IS NULL OR decision = 'approve')
    """)
    # 예약이 승인을 정확히 가리키게 하는 복합 외래키의 대상.
    op.execute("""
        ALTER TABLE approval
          ADD CONSTRAINT approval_reservation_key
          UNIQUE (id, request_id, proposal_id, proposal_version,
                  payload_digest, decision)
    """)

    # 활성 승인 = 승인이면서 철회되지 않은 것. 처리안당 1개, 요청당 1개.
    op.execute("DROP INDEX one_active_approval_per_proposal")
    op.execute("""
        CREATE UNIQUE INDEX one_active_approval_per_proposal
          ON approval (proposal_id)
          WHERE decision = 'approve' AND revoked_at IS NULL
    """)
    op.execute("""
        CREATE UNIQUE INDEX one_active_approval_per_request
          ON approval (request_id)
          WHERE decision = 'approve' AND revoked_at IS NULL
    """)

    op.execute("""
        CREATE TYPE reservation_status AS ENUM ('held', 'consumed', 'released')
    """)
    op.execute("""
        CREATE TABLE stock_reservation (
          id                 bigserial PRIMARY KEY,
          request_id         bigint  NOT NULL,
          proposal_id        bigint  NOT NULL,
          proposal_version   integer NOT NULL,
          payload_digest     text    NOT NULL,
          approval_id        bigint  NOT NULL,
          -- 항상 'approve'. 복합 외래키에 넣어서 거절 기록을 가리키지 못하게 한다.
          approval_decision  approval_decision NOT NULL DEFAULT 'approve',
          assignment_item_id bigint  NOT NULL,
          asset_model_id     bigint  NOT NULL,
          qty                integer NOT NULL,
          status             reservation_status NOT NULL DEFAULT 'held',
          created_at         timestamptz NOT NULL DEFAULT now(),
          resolved_at        timestamptz,
          execution_key      text,

          CONSTRAINT reservation_qty_positive CHECK (qty > 0),
          CONSTRAINT reservation_requires_approving_decision
            CHECK (approval_decision = 'approve'),
          CONSTRAINT reservation_state_shape CHECK (
            (status = 'held'     AND resolved_at IS NULL
                                 AND execution_key IS NULL)
            OR
            (status = 'released' AND resolved_at IS NOT NULL
                                 AND execution_key IS NULL)
            OR
            (status = 'consumed' AND resolved_at IS NOT NULL
                                 AND execution_key IS NOT NULL)),

          CONSTRAINT reservation_approval_key UNIQUE (approval_id),
          CONSTRAINT reservation_execution_key_key UNIQUE (execution_key),

          CONSTRAINT reservation_proposal_fk
            FOREIGN KEY (proposal_id, request_id, proposal_version)
            REFERENCES proposal (id, request_id, version),
          CONSTRAINT reservation_approval_fk
            FOREIGN KEY (approval_id, request_id, proposal_id,
                         proposal_version, payload_digest, approval_decision)
            REFERENCES approval (id, request_id, proposal_id,
                                 proposal_version, payload_digest, decision),
          CONSTRAINT reservation_item_fk
            FOREIGN KEY (assignment_item_id, asset_model_id)
            REFERENCES assignment_item (id, asset_model_id),
          CONSTRAINT reservation_stock_fk
            FOREIGN KEY (asset_model_id) REFERENCES asset_stock (asset_model_id),
          -- execution_key 가 NULL 이면(held, released) 검사하지 않는다 (MATCH SIMPLE).
          CONSTRAINT reservation_execution_fk
            FOREIGN KEY (execution_key, request_id, proposal_version)
            REFERENCES request_execution (execution_key, request_id,
                                          proposal_version)
        )
    """)
    # 요청당 살아 있는(held 또는 consumed) 예약은 하나. 버전이 달라도 마찬가지다.
    op.execute("""
        CREATE UNIQUE INDEX one_live_reservation_per_request
          ON stock_reservation (request_id)
          WHERE status IN ('held', 'consumed')
    """)
    # 예약 가능량 계산(held 합계)용
    op.execute("""
        CREATE INDEX held_reservation_by_model
          ON stock_reservation (asset_model_id) WHERE status = 'held'
    """)
    op.execute("""
        CREATE INDEX held_reservation_by_item
          ON stock_reservation (assignment_item_id) WHERE status = 'held'
    """)


def downgrade() -> None:
    bind = op.get_bind()

    reservations = bind.execute(sa.text(
        "SELECT count(*) FROM stock_reservation")).scalar_one()
    if reservations:
        raise RuntimeError(
            "Downgrade refused: %d reservation row(s) exist. Their history "
            "would be lost. Downgrade is supported only while the reservation "
            "table is empty." % reservations)

    revoked = bind.execute(sa.text(
        "SELECT count(*) FROM approval WHERE revoked_at IS NOT NULL"
    )).scalar_one()
    if revoked:
        raise RuntimeError(
            "Downgrade refused: %d revoked approval(s) exist. The original "
            "index cannot represent them. Downgrade is supported only while no "
            "approval is revoked." % revoked)

    op.execute("DROP TABLE stock_reservation")
    op.execute("DROP TYPE reservation_status")

    op.execute("DROP INDEX one_active_approval_per_request")
    op.execute("DROP INDEX one_active_approval_per_proposal")
    op.execute("""
        CREATE UNIQUE INDEX one_active_approval_per_proposal
          ON approval (proposal_id) WHERE decision = 'approve'
    """)
    op.execute("ALTER TABLE approval DROP CONSTRAINT approval_reservation_key")
    op.execute("ALTER TABLE approval DROP CONSTRAINT approval_only_approve_is_revoked")
    op.execute("ALTER TABLE approval DROP CONSTRAINT approval_revocation_shape")
    op.execute("""
        ALTER TABLE approval
          DROP COLUMN revoke_reason,
          DROP COLUMN revoked_by,
          DROP COLUMN revoked_at
    """)

    op.execute("""
        ALTER TABLE request_execution
          DROP CONSTRAINT request_execution_key_request_version_key
    """)
    op.execute("""
        ALTER TABLE assignment_item DROP CONSTRAINT assignment_item_id_model_key
    """)
    op.execute("""
        ALTER TABLE proposal DROP CONSTRAINT proposal_id_request_version_key
    """)

    op.execute("""
        ALTER TABLE asset_stock
          RENAME CONSTRAINT on_hand_qty_not_negative TO stock_not_negative
    """)
    op.execute("ALTER TABLE asset_stock RENAME COLUMN on_hand_qty TO available_qty")
