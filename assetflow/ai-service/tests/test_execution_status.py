"""응답 유실 뒤 실행 원장을 조회해 결과를 확정한다."""

from app.services.execution import execute_replacement
from tests.seed import approve_pending, seed_base, seed_pending


def _case(db, *, execute=False):
    with db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        case = approve_pending(c, base, seed_pending(c, base, qty=1))
        if execute:
            execute_replacement(
                c, actor_id=base["operator_id"], request_id=case["request_id"],
                proposal_version=1, assignment_item_id=case["item_id"], qty=1,
                execution_key=f"{case['request_id']}:1:replacement",
                payload_digest=case["digest"])
    return base, case


def _status(api, case, user_id):
    return api.get(
        "/executions/status",
        params={"request_id": case["request_id"], "proposal_version": 1},
        headers={"x-user-id": str(user_id)})


def test_status_reports_a_committed_execution(api, head_db):
    base, case = _case(head_db, execute=True)
    response = _status(api, case, base["operator_id"])
    assert response.status_code == 200
    assert response.json() == {
        "execution_key": f"{case['request_id']}:1:replacement",
        "outcome": "registered",
        "request_state": "registered",
        "reservation_status": "consumed",
    }


def test_status_reports_that_a_held_reservation_was_not_executed(api, head_db):
    base, case = _case(head_db)
    response = _status(api, case, base["operator_id"])
    assert response.status_code == 200
    assert response.json()["outcome"] == "not_executed"
    assert response.json()["request_state"] == "ready_to_execute"
    assert response.json()["reservation_status"] == "held"


def test_status_is_limited_to_it_operators(api, head_db):
    base, case = _case(head_db)
    response = _status(api, case, base["employee_id"])
    assert response.status_code == 403


def test_status_returns_a_specific_404(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c)
    response = api.get(
        "/executions/status",
        params={"request_id": 999999, "proposal_version": 1},
        headers={"x-user-id": str(base["operator_id"])})
    assert response.status_code == 404
    assert response.json() == {"detail": "요청 또는 처리안을 찾을 수 없다"}
