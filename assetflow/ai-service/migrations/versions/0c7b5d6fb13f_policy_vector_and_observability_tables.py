"""policy vector and observability tables

Revision ID: 0c7b5d6fb13f
Revises: 1a9547bbd01c
Create Date: 2026-09-18 19:12:41.035104

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0c7b5d6fb13f'
down_revision: Union[str, Sequence[str], None] = '1a9547bbd01c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # 업무 정책 원본
    op.execute("""
        CREATE TABLE policy (
          id             bigserial PRIMARY KEY,
          policy_key     text    NOT NULL,
          version        integer NOT NULL,
          effective_from date    NOT NULL,
          condition_text text    NOT NULL,
          exception_text text,
          required_info  text,
          allowed_action text    NOT NULL,
          is_fictional   boolean NOT NULL DEFAULT true,
          UNIQUE (policy_key, version)
        )
    """)

    # 정책 embedding 조각
    op.execute("""
        CREATE TABLE policy_chunk (
          id          bigserial PRIMARY KEY,
          policy_id   bigint  NOT NULL REFERENCES policy(id),
          chunk_index integer NOT NULL,
          content     text    NOT NULL,
          embedding   vector(1536) NOT NULL,
          embed_model text    NOT NULL,
          UNIQUE (policy_id, chunk_index)
        )
    """)
    op.execute("""
        CREATE INDEX policy_chunk_embedding_idx
          ON policy_chunk USING hnsw (embedding vector_cosine_ops)
    """)

    # AI workflow 실행 추적
    op.execute("""
        CREATE TABLE node_trace (
          id                bigserial PRIMARY KEY,
          run_id            uuid    NOT NULL,
          request_id        bigint  REFERENCES request(id),
          node_name         text    NOT NULL,
          attempt_no        integer NOT NULL DEFAULT 1,
          started_at        timestamptz NOT NULL,
          ended_at          timestamptz,
          latency_ms        integer,
          input_summary     jsonb,
          output_summary    jsonb,
          model             text,
          prompt_tokens     integer,
          completion_tokens integer,
          cost_usd          numeric(10,6),
          outcome           text    NOT NULL,
          error_reason      text,
          CONSTRAINT trace_outcome_values
            CHECK (outcome IN ('ok', 'retried', 'failed')),
          CONSTRAINT trace_attempt_positive CHECK (attempt_no >= 1)
        )
    """)
    op.execute("CREATE INDEX node_trace_run_idx ON node_trace (run_id, started_at)")

    # 누가 무엇을 허용/거부했는지 기록
    op.execute("""
        CREATE TABLE audit_log (
          id                 bigserial PRIMARY KEY,
          actor_id           bigint REFERENCES app_user(id),
          actor_role_claimed text,
          action             text NOT NULL,
          target_type        text NOT NULL,
          target_id          text,
          result             text NOT NULL,
          reason             text,
          at                 timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT result_values CHECK (result IN ('allowed', 'denied'))
        )
    """)


def downgrade() -> None:
    for table in ("audit_log", "node_trace", "policy_chunk", "policy"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP EXTENSION IF EXISTS vector")
