import pytest
from sqlalchemy import text
from app.services.execution import (
    ExecutionRejected,
    execute_replacement,
    mark_outcome_unknown,
    resolve_outcome,
)


@pytest.fixture
def replacement_case(conn):
    """독 2개 지급 · 0개 처리 · 재고 5개 · 승인 대기 요청 1건. AR-01 상황."""
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'kim')"
    ))
    conn.execute(text(
        "INSERT INTO asset_model (code, name, category) "
        "VALUES ('DOCK-01', 'Dock', 'PERIPHERAL')"
    ))
    conn.execute(text(
        "INSERT INTO asset_stock (asset_model_id, available_qty) "
        "VALUES ((SELECT max(id) FROM asset_model), 5)"
    ))
    conn.execute(text(
        "INSERT INTO assignment (employee_id, status, assigned_at) "
        "VALUES ((SELECT max(id) FROM app_user), 'handed_over', now())"
    ))
    item_id = conn.execute(text(
        "INSERT INTO assignment_item "
        "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
        " handover_status, allocated_qty) "
        "VALUES ((SELECT max(id) FROM assignment), (SELECT max(id) FROM asset_model), "
        "        2, 120000, 'delivered', 0) RETURNING id"
    )).scalar_one()
    request_id = conn.execute(text(
        "INSERT INTO request (employee_id, state) "
        "VALUES ((SELECT max(id) FROM app_user), 'ready_to_execute') RETURNING id"
    )).scalar_one()
    # 승인 생성 추가
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name, can_approve) "
        "VALUES ('it_operator', 'op', true)"
    ))
    proposal_id = conn.execute(text(
        "INSERT INTO proposal "
        "(request_id, version, action_type, payload, payload_digest, policy_refs) "
        "VALUES (:request_id, 1, 'replacement', '{}', 'd1', '[]') RETURNING id"
    ), {"request_id": request_id}).scalar_one()
    conn.execute(text(
        "INSERT INTO approval "
        "(request_id, proposal_id, proposal_version, payload_digest, approver_id, decision) "
        "VALUES (:request_id, :proposal_id, 1, 'd1', "
        "        (SELECT id FROM app_user WHERE display_name='op'), 'approve')"
    ), {"request_id": request_id, "proposal_id": proposal_id})
    # return {"item_id": item_id, "request_id": request_id}
    employee_id = conn.execute(text(
        "SELECT id FROM app_user WHERE display_name = 'kim'"
    )).scalar_one()
    operator_id = conn.execute(text(
        "SELECT id FROM app_user WHERE display_name = 'op'"
    )).scalar_one()
    return {"item_id": item_id, "request_id": request_id,
            "employee_id": employee_id, "operator_id": operator_id}

def test_replacement_allocates_and_registers(conn, replacement_case):
    execute_replacement(
        conn,
        actor_id=replacement_case["operator_id"],
        request_id=replacement_case["request_id"],
        proposal_version=1,
        assignment_item_id=replacement_case["item_id"],
        qty=1,
        execution_key="k-1",
        payload_digest="d1",
    )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    stock = conn.execute(text("SELECT available_qty FROM asset_stock")).scalar_one()
    dispatches = conn.execute(text("SELECT count(*) FROM simulated_dispatch")).scalar_one()
    state = conn.execute(text(
        "SELECT state FROM request WHERE id = :id"
    ), {"id": replacement_case["request_id"]}).scalar_one()

    assert allocated == 1
    assert stock == 4
    assert dispatches == 1
    assert state == "registered"


def test_replacement_rejected_when_quantity_exceeds_remaining(conn, replacement_case):
    with pytest.raises(ExecutionRejected, match="미처리 수량"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=replacement_case["item_id"],
            qty=3,
            execution_key="k-1",
            payload_digest="d1",
        )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    dispatches = conn.execute(text("SELECT count(*) FROM simulated_dispatch")).scalar_one()

    assert allocated == 0
    assert dispatches == 0


def test_retry_with_same_key_does_not_allocate_twice(conn, replacement_case):
    args = dict(
        actor_id=replacement_case["operator_id"],
        request_id=replacement_case["request_id"],
        proposal_version=1,
        assignment_item_id=replacement_case["item_id"],
        qty=1,
        execution_key="k-1",
        payload_digest="d1",
    )
    execute_replacement(conn, **args)
    execute_replacement(conn, **args)

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    stock = conn.execute(text("SELECT available_qty FROM asset_stock")).scalar_one()
    dispatches = conn.execute(text("SELECT count(*) FROM simulated_dispatch")).scalar_one()

    assert allocated == 1
    assert stock == 4
    assert dispatches == 1


def test_same_key_with_different_payload_is_rejected(conn, replacement_case):
    args = dict(
        actor_id=replacement_case["operator_id"],
        request_id=replacement_case["request_id"],
        proposal_version=1,
        assignment_item_id=replacement_case["item_id"],
        qty=1,
        execution_key="k-1",
    )
    execute_replacement(conn, payload_digest="d1", **args)

    with pytest.raises(ExecutionRejected, match="다른"):
        execute_replacement(conn, payload_digest="d2", **args)



def test_execution_without_approval_is_rejected(conn, replacement_case):
    """SYS-01: 승인 레코드가 없으면 실행되지 않는다."""
    conn.execute(text("DELETE FROM approval"))

    with pytest.raises(ExecutionRejected, match="승인이 없다"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=replacement_case["item_id"],
            qty=1,
            execution_key="k-1",
            payload_digest="d1",
        )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    assert allocated == 0


def test_execution_with_rejected_approval_is_blocked(conn, replacement_case):
    """SYS-13: 거절된 뒤 실행을 시도해도 막힌다."""
    conn.execute(text("UPDATE approval SET decision = 'reject'"))

    with pytest.raises(ExecutionRejected, match="승인이 없다"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=replacement_case["item_id"],
            qty=1,
            execution_key="k-1",
            payload_digest="d1",
        )

    dispatches = conn.execute(text("SELECT count(*) FROM simulated_dispatch")).scalar_one()
    assert dispatches == 0


def test_approval_for_another_version_does_not_authorise(conn, replacement_case):
    """SYS-02: v1 승인으로 v2 를 실행할 수 없다."""
    with pytest.raises(ExecutionRejected, match="승인이 없다"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=2,
            assignment_item_id=replacement_case["item_id"],
            qty=1,
            execution_key="k-2",
            payload_digest="d1",
        )


def test_other_employee_cannot_execute(conn, replacement_case):
    """SYS-07: 남에게 지급된 자산은 조회도 실행도 되지 않는다."""
    stranger_id = conn.execute(text(
        "INSERT INTO app_user (kind, display_name) "
        "VALUES ('employee', 'stranger') RETURNING id"
    )).scalar_one()

    with pytest.raises(ExecutionRejected, match="실행 권한이 없다"):
        execute_replacement(
            conn,
            actor_id=stranger_id,
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=replacement_case["item_id"],
            qty=1,
            execution_key="k-9",
            payload_digest="d1",
        )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    executions = conn.execute(text("SELECT count(*) FROM request_execution")).scalar_one()

    assert allocated == 0
    assert executions == 0
    # denials 기록 추가
    denials = conn.execute(text(
        "SELECT count(*) FROM audit_log WHERE result = 'denied'"
    )).scalar_one()
    assert denials == 1


def test_proposal_cannot_target_another_persons_asset(conn, replacement_case):
    """검사 2: 처리안이 요청자 아닌 사람의 자산을 가리키면 실행하지 않는다."""
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'other')"
    ))
    conn.execute(text(
        "INSERT INTO assignment (employee_id, status, assigned_at) "
        "VALUES ((SELECT id FROM app_user WHERE display_name='other'), "
        "        'handed_over', now())"
    ))
    other_item_id = conn.execute(text(
        "INSERT INTO assignment_item "
        "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
        " handover_status, allocated_qty) "
        "VALUES ((SELECT max(id) FROM assignment), "
        "        (SELECT max(id) FROM asset_model), 2, 120000, 'delivered', 0) "
        "RETURNING id"
    )).scalar_one()

    with pytest.raises(ExecutionRejected, match="요청자에게 지급된 자산이 아니다"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=other_item_id,
            qty=1,
            execution_key="k-other",
            payload_digest="d1",
        )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": other_item_id}).scalar_one()
    assert allocated == 0

def test_approval_by_unauthorised_operator_does_not_authorise(conn, replacement_case):
    """SYS-17b: 승인 행이 있어도 승인 권한이 없으면 실행되지 않는다."""
    conn.execute(text("UPDATE app_user SET can_approve = false WHERE display_name = 'op'"))

    with pytest.raises(ExecutionRejected, match="승인이 없다"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=replacement_case["item_id"],
            qty=1,
            execution_key="k-1",
            payload_digest="d1",
        )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    assert allocated == 0


def test_execution_blocked_when_cited_policy_version_is_gone(conn, replacement_case):
    """SYS-15: 인용한 규정 버전이 사라지면 자동 실행하지 않는다."""
    conn.execute(text(
        "UPDATE proposal SET policy_refs = "
        "'[{\"policy_key\": \"REPLACE-FAULTY\", \"version\": 1}]'::jsonb"
    ))

    with pytest.raises(ExecutionRejected, match="규정 버전"):
        execute_replacement(
            conn,
            actor_id=replacement_case["operator_id"],
            request_id=replacement_case["request_id"],
            proposal_version=1,
            assignment_item_id=replacement_case["item_id"],
            qty=1,
            execution_key="k-1",
            payload_digest="d1",
        )


def test_execution_proceeds_when_cited_policy_exists(conn, replacement_case):
    """반대쪽: 인용한 규정이 실제로 있으면 통과한다."""
    conn.execute(text(
        "INSERT INTO policy "
        "(policy_key, version, effective_from, condition_text, allowed_action, subject) "
        "VALUES ('REPLACE-FAULTY', 1, '2026-01-01', '고장 확인 시 교체', '교체 처리안', 'replacement')"
    ))
    conn.execute(text(
        "UPDATE proposal SET policy_refs = "
        "'[{\"policy_key\": \"REPLACE-FAULTY\", \"version\": 1}]'::jsonb"
    ))

    execute_replacement(
        conn,
        actor_id=replacement_case["operator_id"],
        request_id=replacement_case["request_id"],
        proposal_version=1,
        assignment_item_id=replacement_case["item_id"],
        qty=1,
        execution_key="k-1",
        payload_digest="d1",
    )

    allocated = conn.execute(text(
        "SELECT allocated_qty FROM assignment_item WHERE id = :id"
    ), {"id": replacement_case["item_id"]}).scalar_one()
    assert allocated == 1



def test_resolve_outcome_finds_a_committed_execution(conn, replacement_case):
    """SYS-04: 응답이 유실돼도 키로 조회하면 기존 결과를 찾는다."""
    execute_replacement(
        conn,
        actor_id=replacement_case["operator_id"],
        request_id=replacement_case["request_id"],
        proposal_version=1,
        assignment_item_id=replacement_case["item_id"],
        qty=1,
        execution_key="k-1",
        payload_digest="d1",
    )

    assert resolve_outcome(conn, execution_key="k-1") == "registered"
    assert resolve_outcome(conn, execution_key="k-없는키") == "not_executed"


def test_mark_outcome_unknown_does_not_claim_success_or_failure(conn, replacement_case):
    """SYS-16: 확인 불가일 때 성공도 실패도 단정하지 않는다."""
    mark_outcome_unknown(conn, request_id=replacement_case["request_id"])

    state = conn.execute(text(
        "SELECT state FROM request WHERE id = :id"
    ), {"id": replacement_case["request_id"]}).scalar_one()
    executions = conn.execute(text("SELECT count(*) FROM request_execution")).scalar_one()

    assert state == "outcome_unknown"
    assert executions == 0