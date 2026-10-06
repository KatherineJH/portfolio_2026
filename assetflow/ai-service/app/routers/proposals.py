"""처리안 목록과 승인."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from app.db import get_conn
from app.deps import current_user
from app.schemas.proposals import ApprovalRequest

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


@router.post("/{proposal_id}/approval")
def decide(
    proposal_id: int,
    body: ApprovalRequest,
    user: dict = Depends(current_user),
    conn: Connection = Depends(get_conn),
):
    """승인은 '지금 저장된 그 처리안'에 결속된다.

    payload_digest 를 본문에서 받지 않고 proposal 테이블에서 읽는 이유다.
    """
    if not user["can_approve"]:
        conn.execute(text(
            "INSERT INTO audit_log "
            "(actor_id, action, target_type, target_id, result, reason) "
            "VALUES (:actor, 'approve', 'proposal', :target, 'denied', "
            "        '승인 권한 없음')"
        ), {"actor": user["id"], "target": str(proposal_id)})
        raise HTTPException(status_code=403, detail="승인 권한이 없다")

    if body.decision not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="decision 은 approve 또는 reject")

    proposal = conn.execute(text(
        "SELECT request_id, version, payload_digest FROM proposal WHERE id = :id"
    ), {"id": proposal_id}).one_or_none()

    if proposal is None:
        raise HTTPException(status_code=404, detail="처리안을 찾을 수 없다")

    conn.execute(text(
        "INSERT INTO approval "
        "(request_id, proposal_id, proposal_version, payload_digest, "
        " approver_id, decision) "
        "VALUES (:request_id, :proposal_id, :version, :digest, :approver, :decision)"
    ), {"request_id": proposal.request_id, "proposal_id": proposal_id,
        "version": proposal.version, "digest": proposal.payload_digest,
        "approver": user["id"], "decision": body.decision})

    next_state = "ready_to_execute" if body.decision == "approve" else "rejected"
    conn.execute(text(
        "UPDATE request SET state = :state, updated_at = now() WHERE id = :id"
    ), {"state": next_state, "id": proposal.request_id})

    return {"request_id": proposal.request_id, "decision": body.decision,
            "approver_id": user["id"]}
