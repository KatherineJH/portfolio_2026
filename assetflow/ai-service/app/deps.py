"""라우터들이 공유하는 의존성.

routers 도 services 도 아닌 자리다. routers/ 에 두면 서비스가 라우터를
import 하게 되어 방향이 거꾸로 된다.
"""

from fastapi import Depends, Header, HTTPException
from sqlalchemy import Connection, text

from app.db import get_conn


def current_user(
    x_user_id: int = Header(...),
    conn: Connection = Depends(get_conn),
) -> dict:
    """인증된 주체를 서버가 확인한다.

    데모용으로 헤더를 인증 대신 쓴다. 실제 서비스라면 JWT 검증이 들어갈
    자리다. 중요한 것은 권한 정보를 요청 본문이 아니라 DB 에서 읽는다는
    점이다 (SYS-17b).
    """
    row = conn.execute(text(
        "SELECT id, kind, can_approve FROM app_user WHERE id = :id"
    ), {"id": x_user_id}).one_or_none()

    if row is None:
        raise HTTPException(status_code=401, detail="인증되지 않은 주체")

    return {"id": row.id, "kind": row.kind, "can_approve": row.can_approve}
