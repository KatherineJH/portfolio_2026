"""처리안 목록과 승인."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from app.db import get_conn
from app.deps import current_user
from app.schemas.proposals import ApprovalRequest
from app.services.approval import ApprovalInputError, decide_proposal

router = APIRouter(prefix="/proposals", tags=["proposals"])

# 이 API 가 무엇을 보여주는 창구인지 서버가 정한다. 아무 상태나 열지 않는다.
PENDING_STATES = {"awaiting_approval", "ready_to_execute"}


@router.get("/pending")
def list_pending(
    state: str = "awaiting_approval",
    conn: Connection = Depends(get_conn),
):
    """상태별 처리안 목록. 판단에 필요한 사실을 함께 돌려준다."""
    if state not in PENDING_STATES:
        raise HTTPException(status_code=400, detail=f"알 수 없는 상태: {state}")

    rows = conn.execute(text(
        "SELECT p.id AS proposal_id, p.request_id, p.version, p.payload, "
        "       p.policy_refs, p.created_at, "
        "       u.display_name AS requester, "
        "       m.code AS asset_code, m.name AS asset_name, "
        "       ai.qty, ai.allocated_qty, ai.qty - ai.allocated_qty AS remaining, "
        "       s.on_hand_qty AS stock "
        "FROM proposal p "
        "JOIN request r ON r.id = p.request_id "
        "JOIN app_user u ON u.id = r.employee_id "
        "JOIN assignment_item ai ON ai.id = (p.payload->>'assignment_item_id')::bigint "
        "JOIN asset_model m ON m.id = ai.asset_model_id "
        "LEFT JOIN asset_stock s ON s.asset_model_id = m.id "
        "WHERE r.state::text = :state "
        "ORDER BY p.created_at DESC"
    ), {"state": state}).mappings().all()

    return {"items": [dict(row) for row in rows]}


# 업무 규칙 거절의 HTTP 상태. 권한만 403 이고 나머지는 상태 충돌(409)이다.
REFUSAL_STATUS = {"no_permission": 403}


@router.post("/{proposal_id}/approval")
def decide(
    proposal_id: int,
    body: ApprovalRequest,
    user: dict = Depends(current_user),
    conn: Connection = Depends(get_conn),
):
    """승인은 '지금 저장된 그 처리안'에 결속되고, 승인하면 그 수량을 예약한다.

    payload_digest 는 본문이 아니라 proposal 테이블에서 읽는다. 업무 규칙으로
    거절한 경우는 HTTPException 이 아니라 결과값을 돌려준다. 예외를 던지면
    get_conn 이 롤백해서 감사 기록과 needs_review 전환이 사라진다.
    """
    if body.proposal_id != proposal_id:
        raise HTTPException(status_code=400,
                            detail="경로와 본문의 proposal_id 가 다르다")

    try:
        result = decide_proposal(conn, actor=user, proposal_id=proposal_id,
                                 decision=body.decision)
    except ApprovalInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    content = asdict(result)
    if result.outcome == "refused":
        return JSONResponse(
            status_code=REFUSAL_STATUS.get(result.reason_code, 409),
            content=content)
    return content
