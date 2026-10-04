# import pytest
# from sqlalchemy import text
# from sqlalchemy.exc import IntegrityError


# def test_allocation_cannot_exceed_handed_over_quantity(conn):
#     """SYS-05a: 총 배분이 지급 수량을 넘을 수 없다."""
#     conn.execute(text(
#         "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'emp')"
#     ))
#     conn.execute(text(
#         "INSERT INTO asset_model (code, name, category) "
#         "VALUES ('DOCK-01', 'Dock', 'PERIPHERAL')"
#     ))
#     conn.execute(text(
#         "INSERT INTO assignment (employee_id, status, assigned_at) "
#         "VALUES ((SELECT id FROM app_user WHERE display_name='emp'), 'handed_over', now())"
#     ))

#     with pytest.raises(IntegrityError, match="allocation_within_qty"):
#         conn.execute(text(
#             "INSERT INTO assignment_item "
#             "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
#             " handover_status, allocated_qty) "
#             "VALUES ((SELECT max(id) FROM assignment), "
#             "        (SELECT max(id) FROM asset_model), "
#             "        2, 120000, 'delivered', 3)"
#         ))


# def test_full_allocation_is_allowed(conn):
#     """allocated_qty == qty 는 '전량 처리 완료'이며 정당한 상태다."""
#     conn.execute(text(
#         "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'emp')"
#     ))
#     conn.execute(text(
#         "INSERT INTO asset_model (code, name, category) "
#         "VALUES ('DOCK-01', 'Dock', 'PERIPHERAL')"
#     ))
#     conn.execute(text(
#         "INSERT INTO assignment (employee_id, status, assigned_at) "
#         "VALUES ((SELECT id FROM app_user WHERE display_name='emp'), 'handed_over', now())"
#     ))

#     conn.execute(text(
#         "INSERT INTO assignment_item "
#         "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
#         " handover_status, allocated_qty) "
#         "VALUES ((SELECT max(id) FROM assignment), "
#         "        (SELECT max(id) FROM asset_model), "
#         "        2, 120000, 'delivered', 2)"
#     ))

#     remaining = conn.execute(text(
#         "SELECT qty - allocated_qty FROM assignment_item"
#     )).scalar_one()
#     assert remaining == 0

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def test_allocation_cannot_exceed_handed_over_quantity(conn, assignment_item_ids):
    """SYS-05a: 총 배분이 지급 수량을 넘을 수 없다."""
    with pytest.raises(IntegrityError, match="allocation_within_qty"):
        conn.execute(text(
            "INSERT INTO assignment_item "
            "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
            " handover_status, allocated_qty) "
            "VALUES (:assignment, :asset_model, 2, 120000, 'delivered', 3)"
        ), assignment_item_ids)


def test_full_allocation_is_allowed(conn, assignment_item_ids):
    """allocated_qty == qty 는 '전량 처리 완료'이며 정당한 상태다."""
    conn.execute(text(
        "INSERT INTO assignment_item "
        "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
        " handover_status, allocated_qty) "
        "VALUES (:assignment, :asset_model, 2, 120000, 'delivered', 2)"
    ), assignment_item_ids)

    remaining = conn.execute(text(
        "SELECT qty - allocated_qty FROM assignment_item"
    )).scalar_one()
    assert remaining == 0


def test_employee_cannot_hold_approval_rights(conn):
    """SYS-17b: 승인 권한은 IT 담당자만 가질 수 있다."""
    with pytest.raises(IntegrityError, match="employee_cannot_approve"):
        conn.execute(text(
            "INSERT INTO app_user (kind, display_name, can_approve) "
            "VALUES ('employee', 'attacker', true)"
        ))


def test_operator_can_hold_approval_rights(conn):
    """반대쪽: IT 담당자는 승인 권한을 가질 수 있어야 한다."""
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name, can_approve) "
        "VALUES ('it_operator', 'op', true)"
    ))

    can_approve = conn.execute(text(
        "SELECT can_approve FROM app_user WHERE display_name = 'op'"
    )).scalar_one()
    assert can_approve is True


APPROVAL_INSERT = (
    "INSERT INTO approval "
    "(request_id, proposal_id, proposal_version, payload_digest, approver_id, decision) "
    "VALUES (:request, :proposal, 1, 'd1', :approver, :decision)"
)


def test_one_proposal_cannot_have_two_approvals(conn, proposal_ids):
    """SYS-02: 한 처리안에 유효한 승인은 하나뿐이다."""
    conn.execute(text(APPROVAL_INSERT),
                 {**proposal_ids, "approver": proposal_ids["op_a"], "decision": "approve"})

    with pytest.raises(IntegrityError, match="one_active_approval_per_proposal"):
        conn.execute(text(APPROVAL_INSERT),
                     {**proposal_ids, "approver": proposal_ids["op_b"], "decision": "approve"})


def test_one_proposal_can_have_many_rejections(conn, proposal_ids):
    """거절은 여러 건 남아야 한다. 부분 인덱스가 approve 에만 걸린 이유다."""
    conn.execute(text(APPROVAL_INSERT),
                 {**proposal_ids, "approver": proposal_ids["op_a"], "decision": "reject"})
    conn.execute(text(APPROVAL_INSERT),
                 {**proposal_ids, "approver": proposal_ids["op_b"], "decision": "reject"})

    count = conn.execute(text(
        "SELECT count(*) FROM approval WHERE decision = 'reject'"
    )).scalar_one()
    assert count == 2


EXECUTION_INSERT = (
    "INSERT INTO request_execution "
    "(execution_key, request_id, proposal_version, action_type, payload_digest, outcome) "
    "VALUES (:key, :request, 1, 'replacement', 'd1', 'registered')"
)


def test_same_execution_key_cannot_be_used_twice(conn, proposal_ids):
    """SYS-03: 응답 유실로 재시도가 와도 등록은 한 번뿐이다."""
    conn.execute(text(EXECUTION_INSERT), {**proposal_ids, "key": "k-1"})

    with pytest.raises(IntegrityError):
        conn.execute(text(EXECUTION_INSERT), {**proposal_ids, "key": "k-1"})


def test_different_key_cannot_repeat_the_same_intent(conn, proposal_ids):
    """키를 새로 만들어도 같은 (요청·버전·액션)은 두 번 실행되지 않는다."""
    conn.execute(text(EXECUTION_INSERT), {**proposal_ids, "key": "k-1"})

    with pytest.raises(IntegrityError):
        conn.execute(text(EXECUTION_INSERT), {**proposal_ids, "key": "k-2"})