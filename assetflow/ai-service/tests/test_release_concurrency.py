"""해제의 동시성 (ADR-002, 4단계).

해제는 요청 행 잠금 -> 예약 행 잠금 순서만 쓴다(항목·재고는 잠그지 않는다).
실행·승인과 같은 요청 잠금으로 줄을 서므로, 같은 요청에서는 둘 중 하나만 이긴다.
"""

import importlib
import threading

from sqlalchemy import text

from app.services.execution import ExecutionRejected, execute_replacement
from tests.racing import (
    WAIT_SECONDS, actor, check_invariants, join_all, run_blocked,
)
from tests.seed import (
    add_item, approve_pending, audit_log_rows, rows, seed_base, seed_pending,
)

REASON = "동시성 테스트용 해제 사유"


def release_service():
    """서비스 모듈은 구현 전에는 없다. 테스트마다 따로 실패하도록 늦게 가져온다."""
    return importlib.import_module("app.services.release")


def approval_service():
    return importlib.import_module("app.services.approval")


def do_release(conn, base, case):
    return release_service().release_reservation(
        conn, actor=actor(base["operator_id"]),
        proposal_id=case["proposal_id"], reason=REASON)


def do_execute(conn, base, case):
    return execute_replacement(
        conn, actor_id=base["operator_id"], request_id=case["request_id"],
        proposal_version=1, assignment_item_id=case["item_id"], qty=case["qty"],
        execution_key=f"{case['request_id']}:1:replacement",
        payload_digest=case["digest"])


def do_approve(conn, base, case):
    return approval_service().decide_proposal(
        conn, actor=actor(base["operator_id"]),
        proposal_id=case["proposal_id"], decision="approve")


def committed_case(db, *, stock=5, item_qty=2, qty=1):
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        case = approve_pending(c, base, seed_pending(c, base, qty=qty))
    return base, case


# ── 순서를 강제한 경쟁 ──────────────────────────────────────────────

def test_a_release_waits_for_an_execution_and_is_refused(head_db):
    base, case = committed_case(head_db)

    _, result = run_blocked(head_db,
                            lambda c: do_execute(c, base, case),
                            lambda c: do_release(c, base, case))

    assert result.outcome == "refused"
    assert result.reason_code == "already_consumed"
    assert rows(head_db, "SELECT status FROM stock_reservation") == [("consumed",)]
    assert rows(head_db, "SELECT revoked_at IS NULL FROM approval") == [(True,)]
    check_invariants(head_db)


def test_an_execution_waits_for_a_release_and_is_refused(head_db):
    base, case = committed_case(head_db)

    def attempt(c):
        try:
            do_execute(c, base, case)
            return "executed"
        except ExecutionRejected as exc:
            return f"rejected: {exc}"

    first, result = run_blocked(head_db,
                                lambda c: do_release(c, base, case), attempt)

    assert first.outcome == "released"
    assert result.startswith("rejected") and "상태" in result
    assert rows(head_db, "SELECT status FROM stock_reservation") == [("released",)]
    assert rows(head_db, "SELECT count(*) FROM request_execution") == [(0,)]
    check_invariants(head_db)


def test_two_releases_of_one_reservation_release_it_once(head_db):
    base, case = committed_case(head_db)

    first, second = run_blocked(head_db,
                                lambda c: do_release(c, base, case),
                                lambda c: do_release(c, base, case))

    assert first.outcome == "released"
    assert second.outcome == "already_released"
    assert len(audit_log_rows(head_db)) == 1
    assert rows(head_db, "SELECT count(*) FROM approval "
                         "WHERE revoked_at IS NOT NULL") == [(1,)]


def test_a_release_rechecks_the_reservation_under_lock(head_db):
    """요청 잠금을 지키지 않는 다른 작성자가 예약을 먼저 풀고 있는 중에 해제가 들어온다.

    해제는 요청을 잠근 뒤 예약을 조회하고(잠금 없이) 곧 FOR UPDATE 로 다시 읽는다.
    그 사이 예약이 풀렸다면 다시 읽을 때 released 로 보여서 아무것도 바꾸지 않고
    already_released 로 끝나야 한다. 다시 읽지 않으면 held 조건의 UPDATE 가 0행을
    바꿔서 오류가 난다.
    """
    base, case = committed_case(head_db)

    def release_without_the_request_lock(c):
        c.execute(text("SELECT 1 FROM stock_reservation WHERE id = :i FOR UPDATE"),
                  {"i": case["reservation_id"]})
        c.execute(text("UPDATE stock_reservation SET status = 'released', "
                       "resolved_at = now() WHERE id = :i"),
                  {"i": case["reservation_id"]})

    _, result = run_blocked(head_db, release_without_the_request_lock,
                            lambda c: do_release(c, base, case))

    assert result.outcome == "already_released"
    assert rows(head_db, "SELECT status FROM stock_reservation") == [("released",)]
    assert rows(head_db, "SELECT revoked_at IS NULL FROM approval") == [(True,)]
    assert audit_log_rows(head_db) == []


def test_racing_releases_and_executions_of_the_same_request_never_deadlock(head_db):
    """같은 요청에서 실행과 해제가 동시에 끼어든다.

    실행은 요청 -> ... -> 예약 순서로 잠근다. 해제가 요청 잠금 없이 예약부터
    잠그면 예약 -> 요청(상태 UPDATE) 순서가 되어 서로를 기다리다 교착이 난다.
    순서를 강제한 경쟁으로는 이것이 드러나지 않는다(먼저 온 쪽이 모든 잠금을 쥐고 있어서
    순환이 생기지 않는다). 그래서 실제로 동시에 출발시키고 오류가 없는지 본다.
    한 요청에서는 실행이 이기거나 해제가 이기고, 둘 다 이길 수는 없다.
    """
    rounds, per_round = 3, 7              # 연결 풀(15개)을 넘지 않게 한 번에 14 스레드
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=40, item_qty=5)    # 모델 코드가 유니크라 한 번만
    for _ in range(rounds):
        with head_db.engine.begin() as c:
            items = [add_item(c, base, qty=5) for _ in range(per_round)]
            cases = [approve_pending(c, base, seed_pending(c, base, qty=1, item_id=i))
                     for i in items]
        jobs = [("execute", case) for case in cases] + [("release", case) for case in cases]
        barrier = threading.Barrier(len(jobs))
        errors, outcomes = [], {}

        def worker(kind, case):
            try:
                with head_db.engine.connect() as conn:
                    tx = conn.begin()
                    barrier.wait(WAIT_SECONDS)
                    try:
                        if kind == "execute":
                            do_execute(conn, base, case)
                            result = "executed"
                        else:
                            result = do_release(conn, base, case).outcome
                    except ExecutionRejected:
                        result = "rejected"
                    tx.commit()
                    outcomes[(kind, case["request_id"])] = result
            except Exception as exc:     # noqa: BLE001  교착은 여기서 드러난다
                errors.append((kind, exc))

        threads = [threading.Thread(target=worker, args=job) for job in jobs]
        for t in threads:
            t.start()
        join_all(threads)

        assert not errors, errors
        for case in cases:
            executed = outcomes[("execute", case["request_id"])] == "executed"
            released = outcomes[("release", case["request_id"])] == "released"
            assert executed != released, (case["request_id"], outcomes)
    check_invariants(head_db)


# ── 해제·실행·승인이 한꺼번에: 교착 없이 불변조건 유지 ──────────────

def test_simultaneous_releases_executions_and_approvals_keep_the_invariants(head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=10, item_qty=5)
        items = [add_item(c, base, qty=5) for _ in range(6)]
        cases = [seed_pending(c, base, qty=1, item_id=item) for item in items]
        approved = [approve_pending(c, base, case) for case in cases[:5]]
        waiting = cases[5]
    r0, r1, r2, r3, r4 = approved

    jobs = [("execute", r0), ("release", r1), ("release", r1),      # 같은 해제 두 번
            ("release", r2), ("execute", r3), ("approve", waiting)]
    barrier = threading.Barrier(len(jobs))
    errors, results = [], []

    def worker(kind, case):
        try:
            with head_db.engine.connect() as conn:
                tx = conn.begin()
                barrier.wait(WAIT_SECONDS)
                action = {"execute": do_execute, "release": do_release,
                          "approve": do_approve}[kind]
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
    released_twice = [r for kind, rid, r in results
                      if kind == "release" and rid == r1["request_id"]]
    assert sorted(r.outcome for r in released_twice) == ["already_released", "released"]
    assert dict(rows(head_db, "SELECT status, count(*) FROM stock_reservation "
                              "GROUP BY status")) == {
        "consumed": 2,      # r0, r3
        "released": 2,      # r1, r2
        "held": 2,          # r4 와 새로 승인된 waiting
    }
    assert rows(head_db, "SELECT count(*) FROM request WHERE state = 'registered'") == [(2,)]
    assert rows(head_db, "SELECT count(*) FROM request WHERE state = 'needs_review'") == [(2,)]
