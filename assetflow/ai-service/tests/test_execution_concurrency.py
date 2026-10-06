"""실행의 동시성 (ADR-002, 3단계).

잠금 순서 request -> assignment_item -> asset_stock -> stock_reservation 을
승인과 실행이 똑같이 쓰는지 확인한다. 순서가 다르면 서로를 기다리다 교착이 난다.
"""

import importlib
import threading

from sqlalchemy import text

from app.services.execution import ExecutionRejected, execute_replacement
from sqlalchemy.exc import OperationalError

from tests.racing import (
    WAIT_SECONDS, actor, check_clean_run_counts, check_invariants, join_all,
    run_blocked, short_lock_timeout,
)
from tests.seed import add_item, approve_pending, rows, seed_base, seed_pending


def approval_service():
    return importlib.import_module("app.services.approval")


def execute(conn, base, case, key=None):
    return execute_replacement(
        conn, actor_id=base["operator_id"], request_id=case["request_id"],
        proposal_version=1, assignment_item_id=case["item_id"], qty=case["qty"],
        execution_key=key or f"{case['request_id']}:1:replacement",
        payload_digest=case["digest"])


def approve(conn, base, case):
    return approval_service().decide_proposal(
        conn, actor=actor(base["operator_id"]),
        proposal_id=case["proposal_id"], decision="approve")


# ── 순서를 강제한 경쟁 ──────────────────────────────────────────────

def test_two_executions_with_the_same_key_consume_once(head_db):
    """같은 키의 동시 실행은 요청 행에서 줄을 선다. 두 번째는 유니크 인덱스에
    부딪히는 대신 첫 번째의 결과를 돌려받는다."""
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        case = approve_pending(c, base, seed_pending(c, base, qty=1))

    first, second = run_blocked(head_db,
                                lambda c: execute(c, base, case),
                                lambda c: execute(c, base, case))

    assert first == second == f"{case['request_id']}:1:replacement"
    assert rows(head_db, "SELECT on_hand_qty FROM asset_stock") == [(4,)]
    assert rows(head_db, "SELECT count(*) FROM simulated_dispatch") == [(1,)]
    assert rows(head_db, "SELECT count(*) FROM request_execution") == [(1,)]
    assert rows(head_db, "SELECT status FROM stock_reservation") == [("consumed",)]


def test_an_approval_attempt_during_an_execution_waits_and_is_refused(head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        case = approve_pending(c, base, seed_pending(c, base, qty=1))

    _, result = run_blocked(head_db,
                            lambda c: execute(c, base, case),
                            lambda c: approve(c, base, case))

    assert result.outcome == "refused"
    assert result.reason_code == "not_awaiting_approval"
    check_invariants(head_db)


def test_an_approval_sees_the_allocation_an_execution_just_committed(head_db):
    """같은 지급 항목을 두고 실행과 다른 요청의 승인이 경쟁한다.

    승인이 지급 항목을 잠그지 않으면 낡은 allocated_qty(0)로 판단해서, 이미
    소비된 수량 위에 또 예약한다.
    """
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        a = approve_pending(c, base, seed_pending(c, base, qty=2))
        b = seed_pending(c, base, qty=1)

    _, result = run_blocked(head_db,
                            lambda c: execute(c, base, a),
                            lambda c: approve(c, base, b))

    assert result.outcome == "refused"
    assert result.reason_code == "insufficient_item_quantity"
    check_invariants(head_db)


def test_the_reservation_is_rechecked_under_lock_before_it_is_consumed(head_db):
    """실행이 지급 항목·재고 잠금을 기다리는 사이, 요청 잠금을 지키지 않는 다른
    작성자가 예약을 해제한다. 실행은 잠근 뒤 예약을 다시 읽어서 거부해야 한다."""
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        case = approve_pending(c, base, seed_pending(c, base, qty=1))
    ids = {"i": case["item_id"], "m": base["model_id"]}

    def hold_item_and_stock(c):
        c.execute(text("SELECT 1 FROM assignment_item WHERE id = :i FOR UPDATE"), ids)
        c.execute(text("SELECT 1 FROM asset_stock WHERE asset_model_id = :m "
                       "FOR UPDATE"), ids)

    def attempt(c):
        try:
            execute(c, base, case)
            return "executed"
        except ExecutionRejected as exc:
            return f"rejected: {exc}"

    waited_for_the_executor = []

    def release_behind_its_back():
        # 실행이 예약을 항목·재고보다 먼저 잠갔다면 이 UPDATE 가 그 잠금을 기다린다.
        # 그러면 실행은 첫 트랜잭션의 커밋을, 이 코드는 실행의 잠금을 기다리는 순환이
        # 되어 영원히 멈춘다. lock_timeout 으로 끊고 잠금 순서 위반으로 진단한다.
        try:
            with head_db.engine.begin() as other:
                short_lock_timeout(other)
                other.execute(text(
                    "UPDATE stock_reservation SET status = 'released', "
                    "resolved_at = now() WHERE id = :id"),
                    {"id": case["reservation_id"]})
        except OperationalError:
            waited_for_the_executor.append(True)

    _, result = run_blocked(head_db, hold_item_and_stock, attempt,
                            between=release_behind_its_back)

    assert not waited_for_the_executor, (
        "실행이 예약을 항목·재고보다 먼저 잠갔다 (잠금 순서 위반)")
    assert result.startswith("rejected") and "예약" in result
    assert rows(head_db, "SELECT status, execution_key FROM stock_reservation") == [
        ("released", None)]
    assert rows(head_db, "SELECT on_hand_qty FROM asset_stock") == [(5,)]
    assert rows(head_db, "SELECT allocated_qty FROM assignment_item") == [(0,)]
    assert rows(head_db, "SELECT count(*) FROM request_execution") == [(0,)]


# ── 승인과 실행이 한꺼번에: 교착 없이 불변조건 유지 ─────────────────

def test_simultaneous_approvals_and_executions_keep_the_invariants(head_db):
    approved_count, new_count = 3, 3
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=10, item_qty=5)
        items = [add_item(c, base, qty=5) for _ in range(approved_count + new_count)]
        cases = [seed_pending(c, base, qty=1, item_id=item) for item in items]
        approved = [approve_pending(c, base, case) for case in cases[:approved_count]]
        waiting = cases[approved_count:]

    jobs = ([("execute", c) for c in approved]
            + [("execute-again", c) for c in approved]     # 같은 키 중복 실행
            + [("approve", c) for c in waiting])
    barrier = threading.Barrier(len(jobs))
    errors = []

    def worker(kind, case):
        try:
            with head_db.engine.connect() as conn:
                tx = conn.begin()
                barrier.wait(WAIT_SECONDS)
                if kind.startswith("execute"):
                    execute(conn, base, case)
                else:
                    approve(conn, base, case)
                tx.commit()
        except Exception as exc:     # noqa: BLE001  교착과 유니크 위반이 여기서 드러난다
            errors.append((kind, exc))

    threads = [threading.Thread(target=worker, args=job) for job in jobs]
    for t in threads:
        t.start()
    join_all(threads)

    assert not errors, errors
    check_invariants(head_db)
    check_clean_run_counts(head_db)   # 이 DB 의 실행은 전부 이 테스트 안에서 일어났다
    assert rows(head_db, "SELECT count(*) FROM request WHERE state = 'registered'") == [(approved_count,)]
    assert rows(head_db, "SELECT on_hand_qty FROM asset_stock") == [(10 - approved_count,)]
    assert rows(head_db, "SELECT count(*) FROM stock_reservation WHERE status = 'held'") == [(new_count,)]
