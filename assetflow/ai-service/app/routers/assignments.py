"""지급 이력 조회."""

from fastapi import APIRouter
from sqlalchemy import text

from app.db import DBConn

router = APIRouter(tags=["assignments"])


@router.get("/employees/{employee_id}/assignments")
def list_assignments(employee_id: int, conn: DBConn):
    """이 임직원이 무엇을 몇 개 받았고 몇 개가 미처리인지.

    inspection 이 'confirmed_faulty' 면 담당자 점검에서 고장이 확인된 것이다.
    """
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
