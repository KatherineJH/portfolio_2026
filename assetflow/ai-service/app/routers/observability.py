"""워크플로 실행 관측 조회. 업무 상태를 변경하지 않는다."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from app.db import get_conn

router = APIRouter(prefix="/observability", tags=["observability"])


@router.get("/runs")
def list_runs(limit: int = 20, conn: Connection = Depends(get_conn)):
    """최근 실행을 run_id 단위로 요약한다."""
    safe_limit = min(max(limit, 1), 100)
    rows = conn.execute(text(
        "SELECT run_id::text AS run_id, min(started_at) AS started_at, "
        "       max(ended_at) AS ended_at, count(*) AS node_count, "
        "       COALESCE(sum(latency_ms), 0) AS total_latency_ms, "
        "       max(model) FILTER (WHERE model IS NOT NULL) AS model, "
        "       COALESCE(sum(prompt_tokens), 0) AS prompt_tokens, "
        "       COALESCE(sum(completion_tokens), 0) AS completion_tokens, "
        "       COALESCE(sum(cost_usd), 0) AS cost_usd, "
        "       bool_or(outcome = 'failed') AS has_failure "
        "FROM node_trace "
        "GROUP BY run_id "
        "ORDER BY min(started_at) DESC "
        "LIMIT :limit"
    ), {"limit": safe_limit}).mappings().all()
    return {"items": [dict(row) for row in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: UUID, conn: Connection = Depends(get_conn)):
    """한 실행의 노드를 시간 순서로 돌려준다."""
    rows = conn.execute(text(
        "SELECT node_name, attempt_no, started_at, ended_at, latency_ms, "
        "       model, prompt_tokens, completion_tokens, cost_usd, "
        "       outcome, error_reason "
        "FROM node_trace "
        "WHERE run_id = :run_id "
        "ORDER BY started_at, id"
    ), {"run_id": run_id}).mappings().all()
    return {"items": [dict(row) for row in rows]}
