"""승인된 처리안의 실행."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.db import DBConn
from app.deps import current_user
from app.schemas.executions import ExecuteRequest
from app.services.execution import ExecutionRejected, execute_replacement, resolve_outcome

router = APIRouter(prefix="/executions", tags=["executions"])


@router.post("")
def execute(
    body: ExecuteRequest,
    conn: DBConn,
    user: dict = Depends(current_user),
):
    """승인된 처리안의 내용대로 실행한다. 클라이언트는 무엇을 할지 정하지 않는다."""
    proposal = conn.execute(text(
        "SELECT payload, payload_digest FROM proposal "
        "WHERE request_id = :request_id AND version = :version"
    ), {"request_id": body.request_id,
        "version": body.proposal_version}).one_or_none()

    if proposal is None:
        raise HTTPException(status_code=404, detail="처리안을 찾을 수 없다")

    # 무엇을 몇 개 처리할지는 승인된 처리안에만 있다.
    payload = proposal.payload
    # 실행 키는 서버가 만든다. 클라이언트가 보낼 수 없다.
    execution_key = f"{body.request_id}:{body.proposal_version}:replacement"

    try:
        execute_replacement(
            conn,
            actor_id=user["id"],
            request_id=body.request_id,
            proposal_version=body.proposal_version,
            assignment_item_id=payload["assignment_item_id"],
            qty=payload["qty"],
            execution_key=execution_key,
            payload_digest=proposal.payload_digest,
        )
    except ExecutionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return {"execution_key": execution_key, "outcome": "registered"}


@router.get("/status")
def execution_status(
    request_id: int,
    proposal_version: int,
    conn: DBConn,
    user: dict = Depends(current_user),
):
    """응답 유실 뒤 원장에서 실행 결과를 확정한다."""
    if user["kind"] != "it_operator":
        raise HTTPException(status_code=403, detail="IT 담당자만 실행 상태를 조회할 수 있다")

    row = conn.execute(text(
        "SELECT r.state AS request_state, p.action_type, "
        "       sr.status AS reservation_status "
        "FROM request r "
        "JOIN proposal p ON p.request_id = r.id AND p.version = :version "
        "LEFT JOIN stock_reservation sr "
        "  ON sr.request_id = r.id AND sr.proposal_version = p.version "
        " AND sr.status IN ('held', 'consumed') "
        "WHERE r.id = :request_id"
    ), {"request_id": request_id, "version": proposal_version}).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="요청 또는 처리안을 찾을 수 없다")

    execution_key = f"{request_id}:{proposal_version}:{row.action_type}"
    return {
        "execution_key": execution_key,
        "outcome": resolve_outcome(conn, execution_key=execution_key),
        "request_state": row.request_state,
        "reservation_status": row.reservation_status,
    }
