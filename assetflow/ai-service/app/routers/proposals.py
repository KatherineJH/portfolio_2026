"""처리안 목록과 승인."""

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import DBConn
from app.deps import current_user
from app.schemas.proposals import ApprovalRequest, ReleaseRequest, ReReviewRequest
from app.services.approval import ApprovalInputError, decide_proposal
from app.services.release import ReleaseInputError, release_reservation
from app.services.rereview import ReReviewInputError, re_review_proposal

router = APIRouter(prefix="/proposals", tags=["proposals"])

# 이 API 가 무엇을 보여주는 창구인지 서버가 정한다. 아무 상태나 열지 않는다.
PENDING_STATES = {"awaiting_approval", "ready_to_execute", "needs_review"}


@router.get("/pending")
def list_pending(
    conn: DBConn,
    state: str = "awaiting_approval",
):
    """상태별 처리안 목록. 판단에 필요한 사실을 함께 돌려준다."""
    if state not in PENDING_STATES:
        raise HTTPException(status_code=400, detail=f"알 수 없는 상태: {state}")

    rows = conn.execute(text(
        "SELECT p.id AS proposal_id, p.request_id, p.version, p.payload, "
        "       p.policy_refs, p.created_at, "
        "       u.display_name AS requester, "
        "       m.code AS asset_code, m.name AS asset_name, "
        "       ai.qty, ai.allocated_qty, "
        "       ai.qty - ai.allocated_qty AS remaining_item_qty, "
        "       COALESCE(ir.held_qty, 0) AS held_item_qty, "
        "       ai.qty - ai.allocated_qty - COALESCE(ir.held_qty, 0) "
        "         AS reservable_item_qty, "
        "       s.on_hand_qty, COALESCE(sr.held_qty, 0) AS held_stock_qty, "
        "       s.on_hand_qty - COALESCE(sr.held_qty, 0) AS reservable_stock_qty, "
        "       latest.status AS reservation_status, "
        "       latest.qty AS reservation_qty "
        "FROM proposal p "
        "JOIN request r ON r.id = p.request_id "
        "JOIN app_user u ON u.id = r.employee_id "
        "JOIN assignment_item ai ON ai.id = (p.payload->>'assignment_item_id')::bigint "
        "JOIN asset_model m ON m.id = ai.asset_model_id "
        "LEFT JOIN asset_stock s ON s.asset_model_id = m.id "
        "LEFT JOIN LATERAL ("
        "  SELECT COALESCE(sum(item_res.qty), 0) AS held_qty "
        "  FROM stock_reservation item_res "
        "  WHERE item_res.assignment_item_id = ai.id AND item_res.status = 'held'"
        ") ir ON true "
        "LEFT JOIN LATERAL ("
        "  SELECT COALESCE(sum(stock_res.qty), 0) AS held_qty "
        "  FROM stock_reservation stock_res "
        "  WHERE stock_res.asset_model_id = m.id AND stock_res.status = 'held'"
        ") sr ON true "
        "LEFT JOIN LATERAL ("
        "  SELECT own_res.status, own_res.qty FROM stock_reservation own_res "
        "  WHERE own_res.proposal_id = p.id "
        "  ORDER BY own_res.created_at DESC, own_res.id DESC LIMIT 1"
        ") latest ON true "
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
    conn: DBConn,
    user: dict = Depends(current_user),
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


@router.post("/{proposal_id}/release")
def release_proposal(
    proposal_id: int,
    body: ReleaseRequest,
    conn: DBConn,
    user: dict = Depends(current_user),
):
    """아직 실행되지 않은 예약을 풀고 그 예약에 연결된 승인을 철회한다.

    업무 규칙으로 거절한 경우는 HTTPException 이 아니라 결과값을 돌려준다.
    예외를 던지면 get_conn 이 롤백해서 감사 기록이 사라진다.
    """
    try:
        result = release_reservation(conn, actor=user, proposal_id=proposal_id,
                                     reason=body.reason)
    except ReleaseInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    content = asdict(result)
    if result.outcome == "refused":
        return JSONResponse(
            status_code=REFUSAL_STATUS.get(result.reason_code, 409),
            content=content)
    return content


@router.post("/{proposal_id}/re-review")
def re_review(
    proposal_id: int,
    body: ReReviewRequest,
    conn: DBConn,
    user: dict = Depends(current_user),
):
    """needs_review 에 머문 요청을 같은 처리안으로 다시 승인 대기로 돌린다.

    승인도 예약도 만들지 않는다. 업무 규칙으로 거절한 경우는 HTTPException 이 아니라
    결과값을 돌려준다. 예외를 던지면 get_conn 이 롤백해서 감사 기록이 사라진다.
    """
    try:
        result = re_review_proposal(conn, actor=user, proposal_id=proposal_id,
                                    reason=body.reason)
    except ReReviewInputError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    content = asdict(result)
    if result.outcome == "refused":
        return JSONResponse(
            status_code=REFUSAL_STATUS.get(result.reason_code, 409),
            content=content)
    return content
