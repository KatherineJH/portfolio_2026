"""승인과 재고 예약 (ADR-002).

승인은 해당 수량을 예약한다. 하나의 트랜잭션에서 검증하고, 승인 기록과 예약을
함께 만든다. 잠금 순서는 항상 request -> assignment_item -> asset_stock 이고
실행 트랜잭션도 같은 순서를 쓴다.

업무 규칙으로 거절한 경우는 예외가 아니라 결과값으로 돌려준다. 예외를 던지면
get_conn 이 트랜잭션 전체를 롤백해서, 함께 커밋해야 하는 needs_review 전환과
감사 기록이 사라진다.
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text

DECISIONS = ("approve", "reject")


class ApprovalInputError(Exception):
    """입력 형식 오류(400)와 없는 처리안(404). 업무 규칙 거절이 아니므로 기록하지 않는다."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ApprovalOutcome:
    outcome: str                      # approved | rejected | refused
    request_id: int | None = None
    approver_id: int | None = None
    decision: str | None = None
    reservation_id: int | None = None
    reason_code: str | None = None    # refused 일 때만
    detail: str | None = None         # refused 일 때만. 화면에 그대로 보여준다.


def decide_proposal(conn: Connection, *, actor: dict, proposal_id: int,
                    decision: str) -> ApprovalOutcome:
    # 권한은 DB 에서 읽은 값(actor)만 믿는다 (SYS-17b).
    if not actor["can_approve"]:
        return _refuse(conn, actor, proposal_id, decision, None,
                       "no_permission", "승인 권한이 없다")

    if decision not in DECISIONS:
        raise ApprovalInputError(400, "decision 은 approve 또는 reject")

    # 처리안은 불변이라 잠금 없이 읽어도 된다.
    proposal = conn.execute(text(
        "SELECT id, request_id, version, action_type, payload, payload_digest "
        "FROM proposal WHERE id = :id"
    ), {"id": proposal_id}).one_or_none()
    if proposal is None:
        raise ApprovalInputError(404, "처리안을 찾을 수 없다")

    # 잠금 1: 요청. 같은 요청의 동시 승인은 여기서 줄을 선다.
    request = conn.execute(text(
        "SELECT id, employee_id, state FROM request WHERE id = :id FOR UPDATE"
    ), {"id": proposal.request_id}).one()

    def refuse(code: str, detail: str) -> ApprovalOutcome:
        return _refuse(conn, actor, proposal_id, decision, request.id, code, detail)

    # 잠근 뒤에 검사한다. 잠그기 전의 값으로 판단하면 두 승인이 모두 통과한다.
    if request.state != "awaiting_approval":
        return refuse("not_awaiting_approval",
                      f"승인 대기 상태가 아니다 (현재 {request.state})")

    active = conn.execute(text(
        "SELECT 1 FROM approval "
        "WHERE request_id = :r AND decision = 'approve' AND revoked_at IS NULL"
    ), {"r": request.id}).first()
    if active is not None:
        return refuse("already_approved", "이미 유효한 승인이 있다")

    # 승인이 없는데 예약만 살아 있는 어긋난 데이터. 승인하면 유일 인덱스에 걸려
    # 500 이 되고, 거절하면 예약이 주인 없이 재고를 계속 막는다. 둘 다 막는다.
    live = conn.execute(text(
        "SELECT 1 FROM stock_reservation "
        "WHERE request_id = :r AND status IN ('held', 'consumed')"
    ), {"r": request.id}).first()
    if live is not None:
        return refuse("already_reserved", "이미 살아 있는 예약이 있다")

    if decision == "reject":
        conn.execute(text(
            "INSERT INTO approval "
            "(request_id, proposal_id, proposal_version, payload_digest, "
            " approver_id, decision) "
            "VALUES (:r, :p, :v, :d, :a, 'reject')"
        ), {"r": request.id, "p": proposal.id, "v": proposal.version,
            "d": proposal.payload_digest, "a": actor["id"]})
        _set_state(conn, request.id, "rejected")
        return ApprovalOutcome("rejected", request_id=request.id,
                               approver_id=actor["id"], decision="reject")

    # ── 승인: 예약하기 전에 처리안이 말이 되는지부터 확인한다 ──
    # 예약은 교체 처리안만 만든다. 비용 청구 초안 등은 재고를 잡지 않는다.
    if proposal.action_type != "replacement":
        return refuse("invalid_proposal",
                      f"교체 처리안만 승인할 수 있다 (현재 {proposal.action_type})")
    item_id, qty = _read_payload(proposal.payload)
    if item_id is None:
        return refuse("invalid_proposal",
                      "처리안에 올바른 교체 동작과 지급 항목, 수량이 없다")

    owner = conn.execute(text(
        "SELECT a.employee_id FROM assignment_item ai "
        "JOIN assignment a ON a.id = ai.assignment_id WHERE ai.id = :id"
    ), {"id": item_id}).one_or_none()
    if owner is None:
        return refuse("invalid_proposal", "처리안이 가리키는 지급 항목이 없다")
    if owner.employee_id != request.employee_id:
        return refuse("item_not_owned_by_requester",
                      "처리안의 지급 항목이 요청자의 것이 아니다")

    # 잠금 2, 3: 지급 항목 -> 재고. 실행 트랜잭션과 같은 순서다.
    # 승인끼리의 경쟁은 재고 잠금 하나로도 줄이 서지만(항목은 모델 하나에 속한다),
    # 지급 항목 잠금을 빼면 안 된다. 실행이 allocated_qty 를 바꾸는 중에 승인이
    # 재고 잠금을 기다리면, 기다리기 전에 읽은 낡은 allocated_qty 로 판단한다.
    # 항목을 먼저 잠그면 그 값은 실행이 커밋한 뒤의 최신 값이다.
    item = conn.execute(text(
        "SELECT qty, allocated_qty, asset_model_id FROM assignment_item "
        "WHERE id = :id FOR UPDATE"
    ), {"id": item_id}).one()
    on_hand = conn.execute(text(
        "SELECT on_hand_qty FROM asset_stock "
        "WHERE asset_model_id = :m FOR UPDATE"
    ), {"m": item.asset_model_id}).scalar_one_or_none() or 0

    # 두 잠금을 쥔 상태에서 계산한다. held 만 수량을 막는다. consumed 는
    # 이미 on_hand 와 allocated_qty 에 반영됐고 released 는 풀렸다.
    held_item = conn.execute(text(
        "SELECT COALESCE(sum(qty), 0) FROM stock_reservation "
        "WHERE assignment_item_id = :id AND status = 'held'"
    ), {"id": item_id}).scalar_one()
    held_stock = conn.execute(text(
        "SELECT COALESCE(sum(qty), 0) FROM stock_reservation "
        "WHERE asset_model_id = :m AND status = 'held'"
    ), {"m": item.asset_model_id}).scalar_one()

    reservable_item = item.qty - item.allocated_qty - held_item
    reservable_stock = on_hand - held_stock

    # 둘 다 모자라면 지급 항목 사유를 먼저 알린다.
    shortage = None
    if qty > reservable_item:
        shortage = ("insufficient_item_quantity",
                    f"지급 항목의 예약 가능 수량 {reservable_item}개, 요청 {qty}개")
    elif qty > reservable_stock:
        shortage = ("insufficient_stock",
                    f"재고의 예약 가능 수량 {reservable_stock}개, 요청 {qty}개")
    if shortage is not None:
        # 승인은 만들지 않는다. 요청은 검토 상태로 보내고, 이 전환은 의도된
        # 업무 결과라서 예외 없이 커밋한다. 감사 기록도 같은 트랜잭션이다.
        _set_state(conn, request.id, "needs_review")
        code, detail = shortage
        return refuse(code, f"{detail}. 요청을 검토 상태로 전환했다")

    approval_id = conn.execute(text(
        "INSERT INTO approval "
        "(request_id, proposal_id, proposal_version, payload_digest, "
        " approver_id, decision) "
        "VALUES (:r, :p, :v, :d, :a, 'approve') RETURNING id"
    ), {"r": request.id, "p": proposal.id, "v": proposal.version,
        "d": proposal.payload_digest, "a": actor["id"]}).scalar_one()

    # 방금 받은 승인 id 로 예약을 만든다. 실패하면 예외가 나가고 승인도 사라진다.
    reservation_id = conn.execute(text(
        "INSERT INTO stock_reservation "
        "(request_id, proposal_id, proposal_version, payload_digest, "
        " approval_id, assignment_item_id, asset_model_id, qty) "
        "VALUES (:r, :p, :v, :d, :ap, :i, :m, :q) RETURNING id"
    ), {"r": request.id, "p": proposal.id, "v": proposal.version,
        "d": proposal.payload_digest, "ap": approval_id, "i": item_id,
        "m": item.asset_model_id, "q": qty}).scalar_one()

    _set_state(conn, request.id, "ready_to_execute")
    return ApprovalOutcome("approved", request_id=request.id,
                           approver_id=actor["id"], decision="approve",
                           reservation_id=reservation_id)


def _read_payload(payload) -> tuple[int | None, int | None]:
    """교체 처리안 payload 에서 지급 항목 id 와 수량을 읽는다. 모양이 틀리면 (None, None)."""
    if not isinstance(payload, dict) or payload.get("action") != "replacement":
        return None, None
    item_id, qty = payload.get("assignment_item_id"), payload.get("qty")
    for value in (item_id, qty):
        # bool 은 int 의 하위 형이라 따로 걸러낸다.
        if isinstance(value, bool) or not isinstance(value, int):
            return None, None
    if qty < 1:
        return None, None
    return item_id, qty


def _set_state(conn: Connection, request_id: int, state: str) -> None:
    conn.execute(text(
        "UPDATE request SET state = CAST(:s AS request_state), "
        "updated_at = now() WHERE id = :id"
    ), {"s": state, "id": request_id})


def _refuse(conn: Connection, actor: dict, proposal_id: int, decision: str,
            request_id: int | None, code: str, detail: str) -> ApprovalOutcome:
    """업무 규칙 거절. 감사 기록을 남기고 결과값으로 돌려준다."""
    conn.execute(text(
        "INSERT INTO audit_log "
        "(actor_id, action, target_type, target_id, result, reason) "
        "VALUES (:actor, :action, 'proposal', :target, 'denied', :reason)"
    ), {"actor": actor["id"],
        "action": decision if decision in DECISIONS else "decide",
        "target": str(proposal_id), "reason": f"{code}: {detail}"})
    return ApprovalOutcome("refused", request_id=request_id,
                           approver_id=actor["id"], decision=decision,
                           reason_code=code, detail=detail)
