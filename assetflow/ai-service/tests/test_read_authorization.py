"""조회 API도 데모 인증(`x-user-id`)으로 주체를 확인한다.

지급 이력은 본인 또는 IT 담당자, 처리 기록과 실행 관측은 IT 담당자만 본다.
요청자 이름·보유 자산·요청 처리 내용이 담긴 응답이기 때문이다.
"""

from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.seed import add_user, seed_base, seed_pending


@pytest.fixture
def ctx(head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c)
        pending = seed_pending(c, base, qty=1)
        run_id = uuid4()
        c.execute(text(
            "INSERT INTO node_trace (run_id, request_id, node_name, started_at, "
            "                        ended_at, latency_ms, outcome) "
            "VALUES (:run, :req, 'route', now(), now(), 5, 'ok')"
        ), {"run": run_id, "req": pending["request_id"]})
        other_employee = add_user(c, "other-employee")
        viewer = add_user(c, "viewer", kind="it_operator")
    return {"operator": base["operator_id"], "employee": base["employee_id"],
            "other_employee": other_employee, "viewer": viewer,
            "request_id": pending["request_id"], "run_id": str(run_id)}


ENDPOINTS = {
    "assignments": lambda x: f"/employees/{x['employee']}/assignments",
    "trace": lambda x: f"/requests/{x['request_id']}/trace",
    "runs": lambda x: "/observability/runs",
    "run": lambda x: f"/observability/runs/{x['run_id']}",
}


def _get(api, ctx, endpoint, user=None):
    headers = {} if user is None else {"x-user-id": str(user)}
    return api.get(ENDPOINTS[endpoint](ctx), headers=headers)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_read_without_an_identity_is_refused(api, ctx, endpoint):
    assert _get(api, ctx, endpoint).status_code == 422


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_read_by_an_unknown_user_is_refused(api, ctx, endpoint):
    response = _get(api, ctx, endpoint, user=999999)
    assert response.status_code == 401


@pytest.mark.parametrize("endpoint,who", [
    ("assignments", "other_employee"),
    ("trace", "employee"),
    ("runs", "employee"),
    ("run", "employee"),
])
def test_a_read_without_the_right_role_is_refused(api, ctx, endpoint, who):
    """요청한 임직원 본인도 처리 기록과 실행 관측은 보지 못한다."""
    response = _get(api, ctx, endpoint, user=ctx[who])
    assert response.status_code == 403
    assert "items" not in response.json()


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("who", ["operator", "viewer"])
def test_it_operators_can_read(api, ctx, endpoint, who):
    """승인 권한 없는 담당자도 조회는 한다. 승인 권한은 변경 API에서 확인한다."""
    response = _get(api, ctx, endpoint, user=ctx[who])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_an_employee_can_read_their_own_assignments(api, ctx):
    response = _get(api, ctx, "assignments", user=ctx["employee"])
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
