import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_conn
from app.main import app
from tests.seed import add_user, seed_base, seed_pending


@pytest.fixture
def client(conn):
    """API 테스트도 테스트 컨테이너를 쓰고 끝나면 롤백한다."""
    app.dependency_overrides[get_conn] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def approval_case(conn):
    """승인 대기 요청 하나. 처리안은 그래프가 쓰는 모양의 payload 를 가진다."""
    base = seed_base(conn)
    pending = seed_pending(conn, base)
    return {
        "proposal_id": pending["proposal_id"],
        "op_yes": base["operator_id"],
        "op_no": add_user(conn, "op-no", kind="it_operator", can_approve=False),
    }


def test_operator_without_permission_is_refused(client, approval_case, conn):
    """SYS-17b: 승인 권한이 없으면 403 이고 승인 행이 생기지 않는다."""
    response = client.post(
        f"/proposals/{approval_case['proposal_id']}/approval",
        headers={"x-user-id": str(approval_case["op_no"])},
        json={"proposal_id": approval_case["proposal_id"], "decision": "approve"},
    )

    assert response.status_code == 403
    approvals = conn.execute(text("SELECT count(*) FROM approval")).scalar_one()
    assert approvals == 0


def test_approver_id_comes_from_the_server_not_the_body(client, approval_case, conn):
    """SYS-17b: 본문으로 승인자를 위조할 수 없다."""
    response = client.post(
        f"/proposals/{approval_case['proposal_id']}/approval",
        headers={"x-user-id": str(approval_case["op_yes"])},
        json={"proposal_id": approval_case["proposal_id"], "decision": "approve",
              "approver_id": 999},
    )

    assert response.status_code == 200
    approver = conn.execute(text("SELECT approver_id FROM approval")).scalar_one()
    assert approver == approval_case["op_yes"]