"""자산 교체 실행. 하나의 트랜잭션에서 검증하고, 승인 때 만든 예약을 소비한다.

실행은 새 재고를 두고 경쟁하지 않는다. 요청과 처리안 버전에 묶인 held 예약을
consumed 로 바꾸면서 재고와 배분량을 반영한다 (ADR-002).

잠금 순서는 승인과 같다: request -> assignment_item -> asset_stock -> stock_reservation.
순서가 다르면 서로를 기다리다 교착이 난다.
"""

from sqlalchemy import Connection, text

# 실행할 수 있는 요청 상태. outcome_unknown 은 결과를 확인한 뒤 같은 키로
# 다시 시도하는 경우다 (ADR-001: 롤백된 실행은 같은 키로 재시도할 수 있다).
EXECUTABLE_STATES = ("ready_to_execute", "outcome_unknown")

RESERVATION_COLUMNS = (
    "id, status, approval_id, proposal_id, proposal_version, payload_digest, "
    "assignment_item_id, asset_model_id, qty"
)


class ExecutionRejected(Exception):
    """실행 전 검증에서 거부됐다. 아무것도 변경되지 않았다."""


def execute_replacement(
    conn: Connection,
    *,
    actor_id: int,
    request_id: int,
    proposal_version: int,
    assignment_item_id: int,
    qty: int,
    execution_key: str,
    payload_digest: str,
) -> str:
    # 검사 1: 실행 권한. 실행은 IT 담당자만 한다.
    actor = conn.execute(text(
        "SELECT kind FROM app_user WHERE id = :id"
    ), {"id": actor_id}).one_or_none()

    if actor is None or actor.kind != "it_operator":
        reason = "실행 권한이 없다"
        _record_denial(conn, actor_id=actor_id,
                       target_id=assignment_item_id, reason=reason)
        raise ExecutionRejected(reason)

    # 잠금 1: 요청. 같은 요청의 동시 실행·승인은 여기서 줄을 선다. 같은 실행 키의
    # 동시 요청도 유니크 인덱스에 부딪히는 대신 여기서 기다렸다가 기존 결과를 본다.
    request = conn.execute(text(
        "SELECT id, employee_id, state FROM request WHERE id = :id FOR UPDATE"
    ), {"id": request_id}).one_or_none()
    if request is None:
        raise ExecutionRejected("요청을 찾을 수 없다")

    # 검사 2: 처리안이 가리키는 자산이 요청자 본인의 것인가.
    owner = conn.execute(text(
        "SELECT a.employee_id FROM assignment_item ai "
        "JOIN assignment a ON a.id = ai.assignment_id WHERE ai.id = :id"
    ), {"id": assignment_item_id}).one_or_none()
    if owner is None:
        raise ExecutionRejected("지급 항목을 찾을 수 없다")
    if owner.employee_id != request.employee_id:
        reason = "요청자에게 지급된 자산이 아니다"
        _record_denial(conn, actor_id=actor_id,
                       target_id=assignment_item_id, reason=reason)
        raise ExecutionRejected(reason)

    # 멱등성: 같은 키가 이미 있으면 재시도다. 두 번 실행하지 않는다. 첫 실행이
    # 끝나면 요청은 registered 라서 아래 상태 가드에 걸리므로, 반드시 그보다 먼저
    # 확인해서 기존 결과를 돌려준다.
    existing = conn.execute(text(
        "SELECT payload_digest FROM request_execution WHERE execution_key = :key"
    ), {"key": execution_key}).one_or_none()

    if existing is not None:
        if existing.payload_digest != payload_digest:
            raise ExecutionRejected("같은 실행 키에 다른 내용이 왔다")
        return execution_key

    if request.state not in EXECUTABLE_STATES:
        raise ExecutionRejected(f"실행할 수 있는 상태가 아니다 (현재 {request.state})")

    # 승인 검증: 철회되지 않은 유효 승인이 있어야 한다. 승인은 특정 처리안 버전에
    # 결속된다. 철회된 승인은 이력으로 남아 있어도 실행 권한이 아니다.
    approval = conn.execute(text(
        "SELECT a.id, a.payload_digest, p.id AS proposal_id, p.action_type "
        "FROM approval a "
        "JOIN proposal p ON p.id = a.proposal_id "
        "JOIN app_user u ON u.id = a.approver_id "
        "WHERE p.request_id = :request_id "
        "  AND p.version = :version "
        "  AND a.decision = 'approve' "
        "  AND a.revoked_at IS NULL "
        "  AND u.can_approve = true"
    ), {"request_id": request_id, "version": proposal_version}).one_or_none()

    if approval is None:
        raise ExecutionRejected("유효한 승인이 없다")
    if approval.payload_digest != payload_digest:
        raise ExecutionRejected("승인 당시 내용과 실행 내용이 다르다")
    if approval.action_type != "replacement":
        raise ExecutionRejected("교체 처리안만 실행할 수 있다")

    # 처리안이 인용한 규정 버전이 지금도 유효한지 확인한다.
    stale = conn.execute(text(
        "SELECT count(*) FROM proposal p, "
        "     jsonb_to_recordset(p.policy_refs) AS r(policy_key text, version int) "
        "LEFT JOIN policy pol "
        "  ON pol.policy_key = r.policy_key AND pol.version = r.version "
        "WHERE p.request_id = :request_id AND p.version = :version "
        "  AND pol.id IS NULL"
    ), {"request_id": request_id, "version": proposal_version}).scalar_one()

    if stale > 0:
        raise ExecutionRejected("인용한 규정 버전이 더 이상 존재하지 않는다")

    # 예약 사전 조회(잠금 없이). 요청 잠금을 쥐고 있으니 이 요청의 예약을 바꾸는
    # 쪽은 우리 뒤에 선다. 잘못된 요청이 경합이 심한 항목·재고 행을 잠그기 전에 걸러낸다.
    reservation = conn.execute(text(
        f"SELECT {RESERVATION_COLUMNS} FROM stock_reservation "
        "WHERE request_id = :r AND status = 'held'"
    ), {"r": request_id}).one_or_none()
    _check_reservation(reservation, approval=approval,
                       proposal_version=proposal_version,
                       payload_digest=payload_digest,
                       assignment_item_id=assignment_item_id, qty=qty)

    # 잠금 2, 3: 지급 항목 -> 재고. 승인 트랜잭션과 같은 순서다.
    item = conn.execute(text(
        "SELECT qty, allocated_qty, asset_model_id FROM assignment_item "
        "WHERE id = :id FOR UPDATE"
    ), {"id": assignment_item_id}).one()

    on_hand = conn.execute(text(
        "SELECT on_hand_qty FROM asset_stock "
        "WHERE asset_model_id = :model FOR UPDATE"
    ), {"model": item.asset_model_id}).scalar_one()

    # 잠금 4: 예약. 요청 잠금이 이미 이 요청의 예약을 지켜 주지만, 요청 잠금을
    # 지키지 않는 다른 작성자가 있어도 안전하도록 소비 직전에 잠그고 다시 읽는다.
    locked = conn.execute(text(
        f"SELECT {RESERVATION_COLUMNS} FROM stock_reservation "
        "WHERE id = :id FOR UPDATE"
    ), {"id": reservation.id}).one()
    if locked.status != "held":
        raise ExecutionRejected(f"유효한 예약이 없다 (예약이 {locked.status} 상태로 바뀌었다)")
    _check_reservation(locked, approval=approval,
                       proposal_version=proposal_version,
                       payload_digest=payload_digest,
                       assignment_item_id=assignment_item_id, qty=qty)

    # 잠근 뒤에 검증한다. 잠그기 전에 읽은 값으로 판단하면 lost update 가 생긴다.
    remaining = item.qty - item.allocated_qty
    if qty > remaining:
        raise ExecutionRejected(f"미처리 수량 {remaining}개, 요청 {qty}개")
    if on_hand < locked.qty:
        raise ExecutionRejected(
            f"실제 보유 수량 {on_hand}개가 예약 수량 {locked.qty}개보다 적다")

    # 한 세이브포인트 안에서 처리한다. 중간에 실패하면 호출자가 예외를 잡고 같은
    # 트랜잭션을 이어가더라도 아무것도 남지 않는다.
    with conn.begin_nested():
        # 예약이 실행 키를 참조하므로 실행 기록을 먼저 넣는다.
        conn.execute(text(
            "INSERT INTO request_execution "
            "(execution_key, request_id, proposal_version, action_type, "
            " payload_digest, outcome) "
            "VALUES (:key, :request_id, :version, 'replacement', :digest, 'registered')"
        ), {"key": execution_key, "request_id": request_id,
            "version": proposal_version, "digest": payload_digest})

        # held 일 때만 소비하고, 바뀐 행이 정확히 하나인지 확인한다.
        consumed = conn.execute(text(
            "UPDATE stock_reservation "
            "SET status = 'consumed', resolved_at = now(), execution_key = :key "
            "WHERE id = :id AND status = 'held'"
        ), {"key": execution_key, "id": locked.id})
        if consumed.rowcount != 1:
            raise ExecutionRejected(
                f"예약을 소비하지 못했다 (바뀐 행 {consumed.rowcount}개)")

        conn.execute(text(
            "UPDATE assignment_item SET allocated_qty = allocated_qty + :qty "
            "WHERE id = :id"
        ), {"qty": qty, "id": assignment_item_id})
        conn.execute(text(
            "UPDATE asset_stock SET on_hand_qty = on_hand_qty - :qty "
            "WHERE asset_model_id = :model"
        ), {"qty": qty, "model": item.asset_model_id})

        conn.execute(text(
            "INSERT INTO simulated_dispatch (execution_key, assignment_item_id, qty) "
            "VALUES (:key, :item, :qty)"
        ), {"key": execution_key, "item": assignment_item_id, "qty": qty})

        conn.execute(text(
            "UPDATE request SET state = 'registered', updated_at = now() "
            "WHERE id = :id"
        ), {"id": request_id})

    return execution_key


def _check_reservation(reservation, *, approval, proposal_version: int,
                       payload_digest: str, assignment_item_id: int,
                       qty: int) -> None:
    """예약이 승인된 처리안과 이번 실행 입력에 정확히 맞는지 확인한다."""
    if reservation is None:
        raise ExecutionRejected("유효한 예약이 없다")
    if (reservation.approval_id != approval.id
            or reservation.proposal_id != approval.proposal_id
            or reservation.proposal_version != proposal_version
            or reservation.payload_digest != payload_digest):
        raise ExecutionRejected("예약이 승인된 처리안과 다르다")
    if reservation.assignment_item_id != assignment_item_id or reservation.qty != qty:
        raise ExecutionRejected("예약한 지급 항목과 수량이 이번 실행과 다르다")


def _record_denial(conn: Connection, *, actor_id: int, target_id: int, reason: str) -> None:
    """거부를 감사 로그에 남긴다. 업무 변경은 없다."""
    conn.execute(text(
        "INSERT INTO audit_log "
        "(actor_id, action, target_type, target_id, result, reason) "
        "VALUES (:actor_id, 'execute_replacement', 'assignment_item', "
        "        :target_id, 'denied', :reason)"
    ), {"actor_id": actor_id, "target_id": str(target_id), "reason": reason})


def resolve_outcome(conn: Connection, *, execution_key: str) -> str:
    """실행 키로 결과를 조회한다.

    호출한 쪽이 응답을 받지 못했을 때 쓴다. 기록이 없으면 실행되지 않은
    것이므로 'not_executed'. 기록이 있으면 그 결과를 그대로 돌려준다.
    조회 자체가 불가능하면 API가 503과 일시적인 unknown 응답을 돌려준다.
    같은 PostgreSQL에 상태를 따로 기록하려 해도 장애 중에는 기록할 수 없다.
    """
    row = conn.execute(text(
        "SELECT outcome FROM request_execution WHERE execution_key = :key"
    ), {"key": execution_key}).one_or_none()

    return row.outcome if row is not None else "not_executed"
