"""요청 접수와 처리 과정 조회."""

import json
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.db import DBConn, engine
from app.deps import current_user
from app.schemas.requests import IntakeRequest
from app.services.graph import build_graph

router = APIRouter(prefix="/requests", tags=["requests"])


def _supports_persisted_intake_idempotency(conn: Connection) -> bool:
    """현재 DB가 후속 intake 멱등성 컬럼을 갖고 있는지 확인한다.

    포트폴리오 데모는 기존 AssetFlow DB도 읽을 수 있어야 한다. 구버전 DB를
    변경하지 않고, 컬럼이 있을 때만 영속 멱등성 경로를 사용한다.
    """
    return bool(conn.execute(text(
        "SELECT count(*) = 2 "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'request' "
        "AND column_name IN ('intake_key', 'intake_response')"
    )).scalar_one())


@router.post("")
def intake(
    body: IntakeRequest,
    conn: DBConn,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: dict = Depends(current_user),
):
    """요청문을 받아 워크플로를 돌린다. 처리안까지만 만들고 실행하지 않는다."""
    supports_idempotency = _supports_persisted_intake_idempotency(conn)

    # 이미 처리한 키면 그때의 응답을 그대로 돌려준다. 그래프를 다시 돌리지 않는다.
    if supports_idempotency and idempotency_key is not None:
        prior = conn.execute(text(
            "SELECT intake_response FROM request WHERE intake_key = :key"
        ), {"key": idempotency_key}).one_or_none()
        if prior is not None and prior.intake_response is not None:
            return prior.intake_response

    # 요청은 사례다. 판단 결과가 무엇이든 행은 먼저 생긴다.
    try:
        if supports_idempotency:
            request_id = conn.execute(text(
                "INSERT INTO request (employee_id, intake_key) "
                "VALUES (:employee_id, :key) RETURNING id"
            ), {"employee_id": user["id"], "key": idempotency_key}).scalar_one()
        else:
            request_id = conn.execute(text(
                "INSERT INTO request (employee_id) VALUES (:employee_id) RETURNING id"
            ), {"employee_id": user["id"]}).scalar_one()
    except IntegrityError as exc:
        # 같은 키가 동시에 도착했다. 조회로는 못 막고 유니크 인덱스가 막는다.
        raise HTTPException(
            status_code=409, detail="같은 멱등성 키의 요청이 처리 중이다"
        ) from exc

    workflow = build_graph(conn, trace_engine=engine)

    result = workflow.invoke({
        "run_id": str(uuid4()),
        "employee_id": user["id"],
        "message": body.message,
        "request_id": request_id,
    })

    response = {
        "run_id": result["run_id"],
        "route": result["route"],
        "reason": result.get("reason", ""),
        "target_item_id": result.get("target_item_id"),
        "missing": result.get("missing", []),
        "policy_refs": result.get("policy_refs", []),
        "request_id": result.get("request_id"),
        "proposal_id": result.get("proposal_id"),
    }

    if supports_idempotency and idempotency_key is not None:
        conn.execute(text(
            "UPDATE request SET intake_response = :body WHERE id = :id"
        ), {"body": json.dumps(response, ensure_ascii=False), "id": request_id})

    return response


@router.get("/{request_id}/trace")
def get_trace(
    request_id: int,
    conn: DBConn,
    user: dict = Depends(current_user),
):
    """이 요청을 처리한 노드들의 실행 기록.

    run_id 는 워크플로 1회 실행을 묶는 키이고 request_id 는 업무 사례를
    가리킨다. request_id 가 있는 행에서 run_id 를 찾아 같은 실행에 속한
    노드를 전부 가져온다.

    같은 요청이 여러 번 실행될 수 있으므로 가장 최근 run_id 를 고른다.
    ORDER BY 가 없으면 DB 가 아무 행이나 준다. IT 담당자만 조회한다.
    """
    if user["kind"] != "it_operator":
        raise HTTPException(status_code=403, detail="IT 담당자만 처리 기록을 조회할 수 있다")

    rows = conn.execute(text(
        "SELECT node_name, attempt_no, latency_ms, model, "
        "       prompt_tokens, completion_tokens, cost_usd, outcome, error_reason "
        "FROM node_trace "
        "WHERE run_id = ("
        "    SELECT run_id FROM node_trace "
        "    WHERE request_id = :request_id "
        "    ORDER BY started_at DESC LIMIT 1"
        ") "
        "ORDER BY started_at"
    ), {"request_id": request_id}).mappings().all()

    return {"items": [dict(row) for row in rows]}
