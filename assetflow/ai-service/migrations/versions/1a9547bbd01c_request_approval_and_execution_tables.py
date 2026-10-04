"""request approval and execution tables

Revision ID: 1a9547bbd01c
Revises: c58e872f0fc1
Create Date: 2026-09-18 17:41:58.953941

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a9547bbd01c'
down_revision: Union[str, Sequence[str], None] = 'c58e872f0fc1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    op.execute("""
        CREATE TYPE request_state AS ENUM (
          'needs_information', 'needs_review', 'awaiting_approval', 'rejected', 
          'ready_to_execute', 'outcome_unknown', 'registered', 'escalated')
    """)

    # 교체 제안, 비용 청구, 사람에게 판단 넘기기
    op.execute("CREATE TYPE action_type AS ENUM ('replacement', 'cost_claim_draft', 'escalate')") 
    
    op.execute("CREATE TYPE approval_decision AS ENUM ('approve', 'reject')")

    op.execute("CREATE TYPE execution_outcome AS ENUM ('registered', 'rejected', 'unknown')")

    op.execute("""
        CREATE TABLE request (
          id            bigserial PRIMARY KEY,
          employee_id   bigint        NOT NULL REFERENCES app_user(id),
          assignment_id bigint        REFERENCES assignment(id),
          state         request_state NOT NULL DEFAULT 'needs_information',
          created_at    timestamptz   NOT NULL DEFAULT now(),
          updated_at    timestamptz   NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE proposal (
          id             bigserial PRIMARY KEY,
          request_id     bigint      NOT NULL REFERENCES request(id),
          version        integer     NOT NULL,
          action_type    action_type NOT NULL,
          payload        jsonb       NOT NULL,
          payload_digest text        NOT NULL,
          policy_refs    jsonb       NOT NULL,
          created_at     timestamptz NOT NULL DEFAULT now(),
          UNIQUE (request_id, version)
        )
    """)

    op.execute("""
        CREATE TABLE approval (
          id               bigserial PRIMARY KEY,
          request_id       bigint  NOT NULL REFERENCES request(id),
          proposal_id      bigint  NOT NULL REFERENCES proposal(id),
          proposal_version integer NOT NULL,
          payload_digest   text    NOT NULL,
          approver_id      bigint  NOT NULL REFERENCES app_user(id),
          decision         approval_decision NOT NULL,
          decided_at       timestamptz NOT NULL DEFAULT now()
        )
    """)
    # 부분 유니크 인덱스:  특정 조건을 만족하는 행에 대해서만 유니크 제약을 적용
    # 한 처리안에 승인은 최대 1건, 거절은 여러 건이어야 하므로 조건부 유니크 인덱스를 생성
    op.execute("""
        CREATE UNIQUE INDEX one_active_approval_per_proposal
          ON approval (proposal_id) WHERE decision = 'approve'
    """)

    op.execute("""
        CREATE TABLE request_execution (
          execution_key    text PRIMARY KEY,
          request_id       bigint      NOT NULL REFERENCES request(id),
          proposal_version integer     NOT NULL,
          action_type      action_type NOT NULL,
          payload_digest   text        NOT NULL,
          outcome          execution_outcome NOT NULL,
          error_reason     text,
          created_at       timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT minimum_scope_blocks_cost_claim_execution
            CHECK (action_type = 'replacement'),
          UNIQUE (request_id, proposal_version, action_type)
        )
    """)
    # proposal.action_type이 replacement인 경우에만, 승인 후 실제 execution 가능.
    # cost_claim_draft/escalate인 경우에는 proposal로는 만들 수 있지만, request_execution에는 넣을 수 없음


    # 승인된 replacement 실행 결과로, 실제로 어떤 assignment_item에 몇 개를 지급/교체 처리했는지 기록하는 테이블
    op.execute("""
        CREATE TABLE simulated_dispatch (
          id                 bigserial PRIMARY KEY,
          execution_key      text    NOT NULL UNIQUE
                                     REFERENCES request_execution(execution_key),
          assignment_item_id bigint  NOT NULL REFERENCES assignment_item(id),
          qty                integer NOT NULL,
          registered_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT dispatch_qty_positive CHECK (qty > 0)
        )
    """)


def downgrade() -> None:
    for table in ("simulated_dispatch", "request_execution",
                  "approval", "proposal", "request"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    for enum_type in ("execution_outcome", "approval_decision",
                      "action_type", "request_state"):
        op.execute(f"DROP TYPE IF EXISTS {enum_type}")
