"""처리안 저장 전 검증. LLM 출력을 그대로 믿지 않는다."""

import pytest
from sqlalchemy import text

from app.services.graph import write_proposal


@pytest.fixture
def state_with_item(conn):
    """독 2개 지급 · 0개 처리 · 점검 완료 상태를 만든다."""
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'kim')"
    ))
    employee_id = conn.execute(text("SELECT max(id) FROM app_user")).scalar_one()
    request_id = conn.execute(text(
        "INSERT INTO request (employee_id) VALUES (:employee_id) RETURNING id"
    ), {"employee_id": employee_id}).scalar_one()
    return {
        "employee_id": employee_id,
        "request_id": request_id,
        "assignments": [
            {"id": 10, "code": "DOCK-01", "qty": 2, "allocated_qty": 0,
             "remaining": 2, "inspection": "confirmed_faulty", "stock": 5},
        ],
        "policy_refs": [{"policy_key": "REPLACE-FAULTY", "version": 1}],
    }


def test_proposal_saved_when_values_match(conn, state_with_item):
    state = {**state_with_item, "proposal": {"assignment_item_id": 10, "qty": 2}}

    result = write_proposal(conn, state)

    assert "proposal_id" in result
    stored = conn.execute(text("SELECT payload FROM proposal")).scalar_one()
    assert stored["qty"] == 2
    assert conn.execute(text(
        "SELECT state::text FROM request WHERE id = :id"
    ), {"id": state["request_id"]}).scalar_one() == "awaiting_approval"


def test_unknown_item_falls_back_to_need_info(conn, state_with_item):
    """LLM 이 이 임직원의 것이 아닌 자산을 골랐다."""
    state = {**state_with_item, "proposal": {"assignment_item_id": 999, "qty": 1}}

    result = write_proposal(conn, state)

    assert result["route"] == "need_info"
    assert conn.execute(text("SELECT count(*) FROM proposal")).scalar_one() == 0


def test_quantity_over_remaining_falls_back_to_need_info(conn, state_with_item):
    """LLM 이 미처리 수량보다 많이 제안했다."""
    state = {**state_with_item, "proposal": {"assignment_item_id": 10, "qty": 5}}

    result = write_proposal(conn, state)

    assert result["route"] == "need_info"
    assert conn.execute(text("SELECT count(*) FROM proposal")).scalar_one() == 0


def test_pending_inspection_falls_back_to_need_review(conn, state_with_item):
    """점검이 안 끝난 자산에 처리안을 만들려 했다."""
    state_with_item["assignments"][0]["inspection"] = "pending"
    state = {**state_with_item, "proposal": {"assignment_item_id": 10, "qty": 1}}

    result = write_proposal(conn, state)

    assert result["route"] == "need_review"
    assert conn.execute(text("SELECT count(*) FROM proposal")).scalar_one() == 0


def test_route_change_recomputes_citation(conn, state_with_item):
    """decide 가 propose 로 골라 둔 인용이 need_review 로 바뀌면 함께 바뀐다."""
    state_with_item["assignments"][0]["inspection"] = "pending"
    state = {
        **state_with_item,
        "target_item_id": 10,
        "proposal": {"assignment_item_id": 10, "qty": 1},
        "policies": [
            {"policy_key": "REPLACE-FAULTY", "version": 1, "subject": "replacement"},
            {"policy_key": "INSPECTION-REQUIRED", "version": 1, "subject": "inspection"},
        ],
    }

    result = write_proposal(conn, state)

    assert result["route"] == "need_review"
    assert result["policy_refs"] == [
        {"policy_key": "INSPECTION-REQUIRED", "version": 1}
    ]