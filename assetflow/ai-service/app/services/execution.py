"""자산 교체 실행. 하나의 트랜잭션에서 검증하고 배분하고 등록한다."""

from sqlalchemy import Connection, text


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

    # 검사 2: 처리안이 가리키는 자산이 요청자 본인의 것인가.
    row = conn.execute(text(
        "SELECT a.employee_id AS owner_id, r.employee_id AS requester_id "
        "FROM assignment_item ai "
        "JOIN assignment a ON a.id = ai.assignment_id "
        "JOIN request r ON r.id = :request_id "
        "WHERE ai.id = :item_id"
    ), {"item_id": assignment_item_id, "request_id": request_id}).one()

    if row.owner_id != row.requester_id:
        reason = "요청자에게 지급된 자산이 아니다"
        _record_denial(conn, actor_id=actor_id,
                       target_id=assignment_item_id, reason=reason)
        raise ExecutionRejected(reason)
    
    # 멱등성 블록: 같은 키가 이미 있으면 재시도다. 두 번 실행하지 않는다.
    existing = conn.execute(text(
        "SELECT payload_digest FROM request_execution WHERE execution_key = :key"
    ), {"key": execution_key}).one_or_none()

    if existing is not None:
        if existing.payload_digest != payload_digest:
            raise ExecutionRejected("같은 실행 키에 다른 내용이 왔다")
        return execution_key
    # 승인 검증 블록: 유효 승인이 있어야 실행한다. 승인은 특정 처리안 버전에 결속된다.
    # JOIN app_user 한 줄과 u.can_approve = true 한 줄이 추가됨.
    approval = conn.execute(text(
        "SELECT a.payload_digest FROM approval a "
        "JOIN proposal p ON p.id = a.proposal_id "
        "JOIN app_user u ON u.id = a.approver_id "
        "WHERE p.request_id = :request_id "
        "  AND p.version = :version "
        "  AND a.decision = 'approve' "
        "  AND u.can_approve = true"
    ), {"request_id": request_id, "version": proposal_version}).one_or_none()

    if approval is None:
        raise ExecutionRejected("유효한 승인이 없다")
    if approval.payload_digest != payload_digest:
        raise ExecutionRejected("승인 당시 내용과 실행 내용이 다르다")

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

    # 잠금 순서는 항상 assignment_item → asset_stock. 역순으로 잠그지 않는다.
    item = conn.execute(text(
        "SELECT qty, allocated_qty, asset_model_id FROM assignment_item "
        "WHERE id = :id FOR UPDATE"
    ), {"id": assignment_item_id}).one()

    stock = conn.execute(text(
        "SELECT available_qty FROM asset_stock "
        "WHERE asset_model_id = :model FOR UPDATE"
    ), {"model": item.asset_model_id}).scalar_one()

    # 잠근 뒤에 검증한다. 잠그기 전에 읽은 값으로 판단하면 lost update 가 생긴다.
    remaining = item.qty - item.allocated_qty
    if qty > remaining:
        raise ExecutionRejected(f"미처리 수량 {remaining}개, 요청 {qty}개")
    if qty > stock:
        raise ExecutionRejected(f"가용 재고 {stock}개, 요청 {qty}개")

    conn.execute(text(
        "UPDATE assignment_item SET allocated_qty = allocated_qty + :qty WHERE id = :id"
    ), {"qty": qty, "id": assignment_item_id})
    conn.execute(text(
        "UPDATE asset_stock SET available_qty = available_qty - :qty "
        "WHERE asset_model_id = :model"
    ), {"qty": qty, "model": item.asset_model_id})

    conn.execute(text(
        "INSERT INTO request_execution "
        "(execution_key, request_id, proposal_version, action_type, "
        " payload_digest, outcome) "
        "VALUES (:key, :request_id, :version, 'replacement', :digest, 'registered')"
    ), {"key": execution_key, "request_id": request_id,
        "version": proposal_version, "digest": payload_digest})

    conn.execute(text(
        "INSERT INTO simulated_dispatch (execution_key, assignment_item_id, qty) "
        "VALUES (:key, :item, :qty)"
    ), {"key": execution_key, "item": assignment_item_id, "qty": qty})

    conn.execute(text(
        "UPDATE request SET state = 'registered', updated_at = now() WHERE id = :id"
    ), {"id": request_id})

    return execution_key


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
    조회 자체가 불가능한 상황은 이 함수가 아니라 호출한 쪽에서
    outcome_unknown 으로 남긴다.
    """
    row = conn.execute(text(
        "SELECT outcome FROM request_execution WHERE execution_key = :key"
    ), {"key": execution_key}).one_or_none()

    return row.outcome if row is not None else "not_executed"


def mark_outcome_unknown(conn: Connection, *, request_id: int) -> None:
    """실행 결과를 확인할 수 없을 때 사람이 확인하도록 남긴다."""
    conn.execute(text(
        "UPDATE request SET state = 'outcome_unknown', updated_at = now() "
        "WHERE id = :id"
    ), {"id": request_id})