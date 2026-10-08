"""재검토: needs_review 에 머문 요청을 같은 처리안으로 다시 승인 대기로 돌린다 (ADR-002).

승인도 예약도 만들지 않는다. 요청 상태만 needs_review -> awaiting_approval 로 바꾸고,
이후 재승인은 승인 서비스가 처리한다. 대상은 해제된 요청과 수량 부족으로 승인이
거절돼 needs_review 가 된 요청이다.

요청 잠금 뒤의 확인 순서는 아래와 같고, 순서가 곧 규칙이다.
  1. 실행됨          already_executed
  2. 교체됨          proposal_replaced
  3. 이미 대기 중    already_awaiting_approval (1, 2 를 통과한 현재 처리안에만 준다)
     대기 중이 아님  not_in_review
  4. 유효한 승인     approval_still_active
  5. held 예약       reservation_still_held

잠금 순서는 request -> stock_reservation 이다. 승인·해제·실행과 같은 요청 잠금으로
줄을 선다. 업무 규칙 거절은 예외가 아니라 결과값으로 돌려준다. 예외를 던지면
get_conn 이 롤백해서 감사 기록이 사라진다.
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text


class ReReviewInputError(Exception):
    """사유 누락(400)과 없는 처리안(404). 업무 규칙 거절이 아니라 기록하지 않는다."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ReReviewOutcome:
    outcome: str                       # re_reviewed | already_awaiting_approval | refused
    request_id: int | None = None
    proposal_id: int | None = None
    reason_code: str | None = None     # refused 일 때만
    detail: str | None = None          # refused 일 때만. 화면에 그대로 보여준다.


def re_review_proposal(conn: Connection, *, actor: dict, proposal_id: int,
                       reason: str) -> ReReviewOutcome:
    # 권한은 DB 에서 읽은 값(actor)만 믿는다 (SYS-17b).
    if not actor["can_approve"]:
        return _refuse(conn, actor, proposal_id, None, "no_permission",
                       "승인 권한이 없다")

    reason = (reason or "").strip()
    if not reason:
        raise ReReviewInputError(400, "재검토 사유가 필요하다")

    # 처리안은 불변이라 잠금 없이 읽어도 된다.
    proposal = conn.execute(text(
        "SELECT id, request_id, version FROM proposal WHERE id = :id"
    ), {"id": proposal_id}).one_or_none()
    if proposal is None:
        raise ReReviewInputError(404, "처리안을 찾을 수 없다")

    # 잠금 1: 요청. 같은 요청의 동시 승인·해제·실행·재검토는 여기서 줄을 선다.
    request = conn.execute(text(
        "SELECT id, state FROM request WHERE id = :id FOR UPDATE"
    ), {"id": proposal.request_id}).one()

    def refuse(code: str, detail: str) -> ReReviewOutcome:
        return _refuse(conn, actor, proposal_id, request.id, code, detail)

    # 1. 실행됨: 요청 상태와 무관하게 되돌릴 수 없다. 이미 awaiting_approval 이거나
    # registered 여도 이 사유가 먼저다. 실행 기록이 있으면 충분하지만(예약 도입 전에
    # 실행된 처리안은 consumed 예약이 없다), consumed 예약도 함께 본다.
    executed = conn.execute(text(
        "SELECT 1 FROM request_execution "
        "WHERE request_id = :r AND proposal_version = :v "
        "UNION ALL "
        "SELECT 1 FROM stock_reservation "
        "WHERE proposal_id = :p AND status = 'consumed' "
        "LIMIT 1"
    ), {"r": request.id, "v": proposal.version, "p": proposal.id}).first()
    if executed is not None:
        return refuse("already_executed", "이미 실행된 처리안은 되돌릴 수 없다")

    # 2. 교체됨: 더 새로운 처리안이 있으면 오래된 처리안은 되돌리지 않는다. 요청이
    # 이미 awaiting_approval 이어도 마찬가지다.
    newer = conn.execute(text(
        "SELECT 1 FROM proposal WHERE request_id = :r AND version > :v LIMIT 1"
    ), {"r": request.id, "v": proposal.version}).first()
    if newer is not None:
        return refuse("proposal_replaced", "더 새로운 처리안이 있다")

    # 3. 1, 2 를 통과한 현재 처리안에 대해서만 상태를 본다.
    if request.state == "awaiting_approval":
        # 두 번째 재검토: 아무것도 바꾸지 않고 기록도 늘리지 않는다.
        return ReReviewOutcome("already_awaiting_approval",
                               request_id=request.id, proposal_id=proposal_id)
    if request.state != "needs_review":
        return refuse("not_in_review",
                      f"검토 상태가 아니다 (현재 {request.state})")

    # 4. 유효한 승인이 남아 있는 어긋난 데이터. 승인이 살아 있는데 대기로 돌리면
    # 이중 승인이 된다.
    active = conn.execute(text(
        "SELECT 1 FROM approval "
        "WHERE request_id = :r AND decision = 'approve' AND revoked_at IS NULL"
    ), {"r": request.id}).first()
    if active is not None:
        return refuse("approval_still_active", "유효한 승인이 아직 남아 있다")

    # 5. held 예약이 남아 있는 어긋난 데이터. 요청 -> 예약 순서로 잠근다.
    held = conn.execute(text(
        "SELECT id FROM stock_reservation "
        "WHERE request_id = :r AND status = 'held' FOR UPDATE"
    ), {"r": request.id}).first()
    if held is not None:
        return refuse("reservation_still_held", "풀리지 않은 예약이 아직 남아 있다")

    # 한 세이브포인트 안에서 처리한다. 감사 기록이 실패하면 상태 전환도 함께 사라진다.
    with conn.begin_nested():
        # needs_review 일 때만 바꾸고, 바뀐 행이 정확히 하나인지 확인한다.
        changed = conn.execute(text(
            "UPDATE request SET state = 'awaiting_approval', updated_at = now() "
            "WHERE id = :id AND state = 'needs_review'"
        ), {"id": request.id})
        if changed.rowcount != 1:
            raise RuntimeError(
                f"요청 {request.id} 의 상태를 바꾸지 못했다 (바뀐 행 {changed.rowcount}개)")

        conn.execute(text(
            "INSERT INTO audit_log "
            "(actor_id, action, target_type, target_id, result, reason) "
            "VALUES (:actor, 're_review', 'proposal', :target, 'allowed', :reason)"
        ), {"actor": actor["id"], "target": str(proposal_id),
            "reason": f"재검토 사유: {reason}"})

    return ReReviewOutcome("re_reviewed", request_id=request.id,
                           proposal_id=proposal_id)


def _refuse(conn: Connection, actor: dict, proposal_id: int,
            request_id: int | None, code: str, detail: str) -> ReReviewOutcome:
    """업무 규칙 거절. 감사 기록을 남기고 결과값으로 돌려준다."""
    conn.execute(text(
        "INSERT INTO audit_log "
        "(actor_id, action, target_type, target_id, result, reason) "
        "VALUES (:actor, 're_review', 'proposal', :target, 'denied', :reason)"
    ), {"actor": actor["id"], "target": str(proposal_id),
        "reason": f"{code}: {detail}"})
    return ReReviewOutcome("refused", request_id=request_id, proposal_id=proposal_id,
                           reason_code=code, detail=detail)
