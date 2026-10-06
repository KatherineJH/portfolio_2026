"""승인한 예약의 수동 해제와 승인 철회 (ADR-002).

아직 실행되지 않은 예약을 풀고, 그 예약에 연결된 승인을 철회하고, 요청을
needs_review 로 돌린다. 승인 기록은 지우지 않는다. 한 트랜잭션에서 모두 처리한다.

잠금 순서는 request -> stock_reservation 이다. 예약 가능량을 늘리기만 하므로
지급 항목과 재고는 잠그지 않는다. 승인·실행과 같은 요청 잠금으로 줄을 선다.

업무 규칙으로 거절한 경우는 예외가 아니라 결과값으로 돌려준다. 예외를 던지면
get_conn 이 롤백해서 감사 기록이 사라진다.
"""

from dataclasses import dataclass

from sqlalchemy import Connection, text


class ReleaseInputError(Exception):
    """사유 누락(400)과 없는 처리안(404). 업무 규칙 거절이 아니라 기록하지 않는다."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ReleaseOutcome:
    outcome: str                       # released | already_released | refused
    request_id: int | None = None
    proposal_id: int | None = None
    reservation_id: int | None = None
    reason_code: str | None = None     # refused 일 때만
    detail: str | None = None          # refused 일 때만. 화면에 그대로 보여준다.


def release_reservation(conn: Connection, *, actor: dict, proposal_id: int,
                        reason: str) -> ReleaseOutcome:
    # 권한은 DB 에서 읽은 값(actor)만 믿는다 (SYS-17b).
    if not actor["can_approve"]:
        return _refuse(conn, actor, proposal_id, None, "no_permission",
                       "승인 권한이 없다")

    reason = (reason or "").strip()
    if not reason:
        raise ReleaseInputError(400, "해제 사유가 필요하다")

    # 처리안은 불변이라 잠금 없이 읽어도 된다.
    proposal = conn.execute(text(
        "SELECT id, request_id FROM proposal WHERE id = :id"
    ), {"id": proposal_id}).one_or_none()
    if proposal is None:
        raise ReleaseInputError(404, "처리안을 찾을 수 없다")

    # 잠금 1: 요청. 같은 요청의 동시 실행·승인·해제는 여기서 줄을 선다.
    request = conn.execute(text(
        "SELECT id, state FROM request WHERE id = :id FOR UPDATE"
    ), {"id": proposal.request_id}).one()

    def refuse(code: str, detail: str) -> ReleaseOutcome:
        return _refuse(conn, actor, proposal_id, request.id, code, detail)

    # 지정한 처리안의 가장 최근 예약. 요청 단위로 찾으면 같은 요청의 다른 처리안
    # 예약을 풀 수 있다. 같은 시각이면 id 가 큰 쪽이 최신이다.
    latest = conn.execute(text(
        "SELECT id FROM stock_reservation WHERE proposal_id = :p "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    ), {"p": proposal_id}).one_or_none()
    if latest is None:
        return refuse("no_reservation", "이 처리안에 예약이 없다")

    # 잠금 2: 예약. 요청 잠금이 이미 지켜 주지만, 요청 잠금을 지키지 않는 다른
    # 작성자가 있어도 안전하도록 잠그고 다시 읽는다.
    reservation = conn.execute(text(
        "SELECT id, status, approval_id FROM stock_reservation "
        "WHERE id = :id FOR UPDATE"
    ), {"id": latest.id}).one()

    if reservation.status == "consumed":
        return refuse("already_consumed", "이미 실행되어 소비된 예약은 풀 수 없다")
    if reservation.status == "released":
        # 두 번째 해제: 아무것도 바꾸지 않고 기록도 늘리지 않는다.
        return ReleaseOutcome("already_released", request_id=request.id,
                              proposal_id=proposal_id,
                              reservation_id=reservation.id)
    if request.state != "ready_to_execute":
        # outcome_unknown 은 실행 결과를 먼저 확인해야 한다 (6단계).
        return refuse("not_ready_to_execute",
                      f"실행 대기 상태가 아니다 (현재 {request.state})")

    # 한 세이브포인트 안에서 처리한다. 중간에 실패하면 호출자가 예외를 잡고 같은
    # 트랜잭션을 이어가더라도 아무것도 남지 않는다.
    with conn.begin_nested():
        # held 일 때만 풀고, 바뀐 행이 정확히 하나인지 확인한다.
        released = conn.execute(text(
            "UPDATE stock_reservation SET status = 'released', resolved_at = now() "
            "WHERE id = :id AND status = 'held'"
        ), {"id": reservation.id})
        if released.rowcount != 1:
            raise RuntimeError(
                f"예약 {reservation.id} 을 풀지 못했다 (바뀐 행 {released.rowcount}개)")

        # 철회하는 것은 이 예약에 연결된 승인 하나뿐이다. 같은 처리안의 다른 승인과
        # 거절 기록, 이미 철회된 승인의 사유·시각은 건드리지 않는다.
        revoked = conn.execute(text(
            "UPDATE approval SET revoked_at = now(), revoked_by = :actor, "
            "revoke_reason = :reason "
            "WHERE id = :id AND revoked_at IS NULL"
        ), {"actor": actor["id"], "reason": reason, "id": reservation.approval_id})
        if revoked.rowcount != 1:
            raise RuntimeError(
                f"승인 {reservation.approval_id} 을 철회하지 못했다 "
                f"(바뀐 행 {revoked.rowcount}개)")

        conn.execute(text(
            "UPDATE request SET state = 'needs_review', updated_at = now() "
            "WHERE id = :id"
        ), {"id": request.id})

        conn.execute(text(
            "INSERT INTO audit_log "
            "(actor_id, action, target_type, target_id, result, reason) "
            "VALUES (:actor, 'release', 'proposal', :target, 'allowed', :reason)"
        ), {"actor": actor["id"], "target": str(proposal_id),
            "reason": f"예약 {reservation.id} 해제: {reason}"})

    return ReleaseOutcome("released", request_id=request.id,
                          proposal_id=proposal_id, reservation_id=reservation.id)


def _refuse(conn: Connection, actor: dict, proposal_id: int,
            request_id: int | None, code: str, detail: str) -> ReleaseOutcome:
    """업무 규칙 거절. 감사 기록을 남기고 결과값으로 돌려준다."""
    conn.execute(text(
        "INSERT INTO audit_log "
        "(actor_id, action, target_type, target_id, result, reason) "
        "VALUES (:actor, 'release', 'proposal', :target, 'denied', :reason)"
    ), {"actor": actor["id"], "target": str(proposal_id),
        "reason": f"{code}: {detail}"})
    return ReleaseOutcome("refused", request_id=request_id, proposal_id=proposal_id,
                          reason_code=code, detail=detail)
