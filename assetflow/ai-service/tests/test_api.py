import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_conn
from app.main import app


@pytest.fixture
def client(conn):
    """API 테스트도 테스트 컨테이너를 쓰고 끝나면 롤백한다."""
    app.dependency_overrides[get_conn] = lambda: conn
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def approval_case(conn):
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name, can_approve) "
        "VALUES ('it_operator', 'op-yes', true), ('it_operator', 'op-no', false)"
    ))
    conn.execute(text(
        "INSERT INTO request (employee_id, state) "
        "VALUES ((SELECT id FROM app_user WHERE display_name='op-yes'), "
        "        'awaiting_approval')"
    ))
    proposal_id = conn.execute(text(
        "INSERT INTO proposal "
        "(request_id, version, action_type, payload, payload_digest, policy_refs) "
        "VALUES ((SELECT max(id) FROM request), 1, 'replacement', '{}', 'd1', '[]') "
        "RETURNING id"
    )).scalar_one()
    return {
        "proposal_id": proposal_id,
        "op_yes": conn.execute(text(
            "SELECT id FROM app_user WHERE display_name='op-yes'")).scalar_one(),
        "op_no": conn.execute(text(
            "SELECT id FROM app_user WHERE display_name='op-no'")).scalar_one(),
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