"""지급 이력 조회."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.db import DBConn
from app.deps import current_user

router = APIRouter(tags=["assignments"])


@router.get("/employees/{employee_id}/assignments")
def list_assignments(
    employee_id: int,
    conn: DBConn,
    user: dict = Depends(current_user),
):
    """이 임직원이 무엇을 몇 개 받았고 몇 개가 미처리인지.

    inspection 이 'confirmed_faulty' 면 담당자 점검에서 고장이 확인된 것이다.
    본인 또는 IT 담당자만 조회한다.
    """
    if user["id"] != employee_id and user["kind"] != "it_operator":
        raise HTTPException(status_code=403,
                            detail="본인 또는 IT 담당자만 지급 이력을 조회할 수 있다")

    rows = conn.execute(text(
        "SELECT ai.id, m.code, m.name, ai.qty, ai.allocated_qty, "
        "       ai.qty - ai.allocated_qty AS remaining, "
        "       i.status AS inspection "
        "FROM assignment_item ai "
        "JOIN assignment a ON a.id = ai.assignment_id "
        "JOIN asset_model m ON m.id = ai.asset_model_id "
        "LEFT JOIN inspection i ON i.assignment_item_id = ai.id "
        "WHERE a.employee_id = :employee_id "
        "ORDER BY m.code"
    ), {"employee_id": employee_id}).mappings().all()

    return {"items": [dict(row) for row in rows]}
