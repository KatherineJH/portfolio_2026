"""승인된 처리안의 실행."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from app.db import get_conn
from app.deps import current_user
from app.schemas.executions import ExecuteRequest
from app.services.execution import ExecutionRejected, execute_replacement

router = APIRouter(prefix="/executions", tags=["executions"])


@router.post("")
def execute(
    body: ExecuteRequest,
    user: dict = Depends(current_user),
    conn: Connection = Depends(get_conn),
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
