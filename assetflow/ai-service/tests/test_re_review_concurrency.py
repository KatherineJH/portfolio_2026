"""재검토의 동시성 (ADR-002, 5단계).

재검토는 요청 행 잠금 -> 예약 행 잠금 순서만 쓴다. 승인·해제·실행과 같은 요청 잠금으로
줄을 서므로, 같은 요청에서는 순서대로 하나씩 처리된다.
"""

import importlib
import threading

from sqlalchemy import text

from tests.racing import (
    WAIT_SECONDS, actor, check_invariants, join_all, run_blocked,
)
from tests.seed import (
    add_item, approve_pending, audit_log_rows, revoke, rows, seed_base, seed_pending,
)

REASON = "동시성 테스트용 사유"


def service(name):
    """서비스 모듈은 구현 전에는 없다. 테스트마다 따로 실패하도록 늦게 가져온다."""
    return importlib.import_module(f"app.services.{name}")


def do_re_review(conn, base, case):
    return service("rereview").re_review_proposal(
        conn, actor=actor(base["operator_id"]),
        proposal_id=case["proposal_id"], reason=REASON)


def do_release(conn, base, case):
    return service("release").release_reservation(
        conn, actor=actor(base["operator_id"]),
        proposal_id=case["proposal_id"], reason=REASON)


def do_approve(conn, base, case):
    return service("approval").decide_proposal(
        conn, actor=actor(base["operator_id"]),
        proposal_id=case["proposal_id"], decision="approve")


def make_released(c, base, case):
    """해제된 상태를 직접 만든다: 예약 released, 승인 철회, 요청 needs_review."""
    c.execute(text("UPDATE stock_reservation SET status = 'released', "
                   "resolved_at = now() WHERE id = :i"), {"i": case["reservation_id"]})
    revoke(c, base, case["approval_id"], "오래된 사유")
    c.execute(text("UPDATE request SET state = 'needs_review' WHERE id = :r"),
              {"r": case["request_id"]})


def released_case(db, *, stock=5, item_qty=2):
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        case = approve_pending(c, base, seed_pending(c, base, qty=1))
        make_released(c, base, case)
    return base, case


# ── 순서를 강제한 경쟁 ──────────────────────────────────────────────

def test_two_re_reviews_change_the_state_once(head_db):
    base, case = released_case(head_db)

    first, second = run_blocked(head_db,
                                lambda c: do_re_review(c, base, case),
                                lambda c: do_re_review(c, base, case))

    assert first.outcome == "re_reviewed"
    assert second.outcome == "already_awaiting_approval"
    assert rows(head_db, "SELECT state FROM request") == [("awaiting_approval",)]
    assert len(audit_log_rows(head_db)) == 1


def test_a_re_review_waits_for_a_release_and_then_succeeds(head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        case = approve_pending(c, base, seed_pending(c, base, qty=1))

    first, second = run_blocked(head_db,
                                lambda c: do_release(c, base, case),
                                lambda c: do_re_review(c, base, case))

    assert first.outcome == "released"
    assert second.outcome == "re_reviewed"
    assert rows(head_db, "SELECT state FROM request") == [("awaiting_approval",)]
    check_invariants(head_db)


def test_an_approval_waits_for_a_re_review_and_is_then_approved(head_db):
    """재검토가 요청 잠금을 쥔 동안 온 승인은 줄을 서고, 재검토가 커밋된 뒤에는 통과한다."""
    base, case = released_case(head_db)

    first, second = run_blocked(head_db,
                                lambda c: do_re_review(c, base, case),
                                lambda c: do_approve(c, base, case))

    assert first.outcome == "re_reviewed"
    assert second.outcome == "approved"
    assert rows(head_db, "SELECT state FROM request") == [("ready_to_execute",)]
    assert rows(head_db, "SELECT status FROM stock_reservation ORDER BY id") == [
        ("released",), ("held",)]
    check_invariants(head_db)


# ── 해제·재검토·승인이 같은 요청에 한꺼번에: 교착 없이 일관됨 ──────

def test_racing_re_reviews_and_approvals_of_the_same_request_stay_consistent(head_db):
    requests = 5                              # 5 요청 x 3 스레드 = 15, 연결 풀(15개) 이내
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=20, item_qty=5)
        cases = []
        for _ in range(requests):
            item = add_item(c, base, qty=5)
            case = approve_pending(c, base, seed_pending(c, base, qty=1, item_id=item))
            make_released(c, base, case)
            cases.append(case)

    jobs = ([("re-review", c) for c in cases] + [("re-review", c) for c in cases]
            + [("approve", c) for c in cases])
    barrier = threading.Barrier(len(jobs))
    errors, results = [], []

    def worker(kind, case):
        try:
            with head_db.engine.connect() as conn:
                tx = conn.begin()
                barrier.wait(WAIT_SECONDS)
                action = do_re_review if kind == "re-review" else do_approve
                results.append((kind, case["request_id"], action(conn, base, case)))
                tx.commit()
        except Exception as exc:     # noqa: BLE001  교착과 유니크 위반이 여기서 드러난다
            errors.append((kind, exc))

    threads = [threading.Thread(target=worker, args=job) for job in jobs]
    for t in threads:
        t.start()
    join_all(threads)

    assert not errors, errors
    check_invariants(head_db)
    for case in cases:
        mine = [r for kind, rid, r in results
                if kind == "re-review" and rid == case["request_id"]]
        # 한 요청에서 상태를 바꾸는 재검토는 정확히 한 번이다.
        assert [r.outcome for r in mine].count("re_reviewed") == 1, mine
        # 나머지 하나의 유효한 결과는 둘이다. 승인보다 먼저 왔다면 이미 대기 중이고,
        # 승인이 통과해 ready_to_execute 가 된 뒤에 왔다면 검토 상태가 아니다.
        other = next(r for r in mine if r.outcome != "re_reviewed")
        assert (other.outcome == "already_awaiting_approval"
                or (other.outcome == "refused" and other.reason_code == "not_in_review")), other
        approval = next(r for kind, rid, r in results
                        if kind == "approve" and rid == case["request_id"])
        # 승인은 재검토보다 앞서 오면 거절되고, 뒤에 오면 통과한다. 둘 다 일관된 끝 상태다.
        assert approval.outcome in ("approved", "refused")
        if approval.outcome == "refused":
            assert approval.reason_code == "not_awaiting_approval"
    states = dict(rows(head_db, "SELECT state, count(*) FROM request GROUP BY state"))
    assert set(states) <= {"awaiting_approval", "ready_to_execute"}
    assert sum(states.values()) == requests
    held = rows(head_db, "SELECT count(*) FROM stock_reservation WHERE status = 'held'")
    assert held == [(states.get("ready_to_execute", 0),)]
    # 성공한 재검토 기록은 요청마다 정확히 하나다
    assert rows(head_db, "SELECT count(*) FROM audit_log "
                         "WHERE action = 're_review' AND result = 'allowed'") == [(requests,)]
