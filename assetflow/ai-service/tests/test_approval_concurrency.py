"""승인 + 예약의 동시성 (ADR-002, 2단계).

서비스 함수를 별도 연결 두 개로 직접 호출한다. 첫 트랜잭션이 잠금을 쥔 채
커밋하지 않고, 두 번째가 실제로 잠금에서 막힌 것을 확인한 뒤 첫 트랜잭션을
커밋한다(tests/racing.py). 우연히 겹치기를 기대하지 않으므로 불안정하지 않다.
"""

import importlib
import threading

from sqlalchemy import text

from tests.racing import WAIT_SECONDS, actor, join_all, run_blocked
from tests.seed import add_item, seed_base, seed_pending


def service():
    """서비스 모듈은 구현 전에는 없다. 테스트마다 따로 실패하도록 늦게 가져온다."""
    return importlib.import_module("app.services.approval")


def setup(db, *, stock, item_qty, requests, shared_item=False):
    """요청 `requests`개(각 수량 1). shared_item 이 아니면 요청마다 항목을 따로 둔다."""
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        proposals = []
        for _ in range(requests):
            item = base["item_id"] if shared_item else add_item(c, base, qty=item_qty)
            proposals.append(seed_pending(c, base, qty=1, item_id=item))
    return base, proposals


def race(db, base, first, second, *, first_decision="approve",
         second_decision="approve"):
    """first 가 잠금을 쥔 채 second 가 막힌 것을 확인한 뒤 first 를 커밋한다."""
    svc = service()
    return run_blocked(
        db,
        lambda c: svc.decide_proposal(
            c, actor=actor(base["operator_id"]),
            proposal_id=first["proposal_id"], decision=first_decision),
        lambda c: svc.decide_proposal(
            c, actor=actor(base["operator_id"]),
            proposal_id=second["proposal_id"], decision=second_decision))


def held(db):
    with db.engine.connect() as c:
        return c.execute(text(
            "SELECT count(*) FROM stock_reservation WHERE status = 'held'"
        )).scalar_one()


# ── 순서를 강제한 경쟁 ──────────────────────────────────────────────

def test_two_requests_cannot_both_take_the_last_stock_unit(head_db):
    base, (a, b) = setup(head_db, stock=1, item_qty=5, requests=2)

    first, second = race(head_db, base, a, b)

    assert first.outcome == "approved"
    assert second.outcome == "refused"
    assert second.reason_code == "insufficient_stock"
    assert held(head_db) == 1


def test_two_requests_cannot_both_take_the_last_item_unit(head_db):
    base, (a, b) = setup(head_db, stock=10, item_qty=1, requests=2,
                         shared_item=True)

    first, second = race(head_db, base, a, b)

    assert first.outcome == "approved"
    assert second.outcome == "refused"
    assert second.reason_code == "insufficient_item_quantity"
    assert held(head_db) == 1


def test_a_duplicate_approval_of_one_request_waits_and_is_refused(head_db):
    base, (a,) = setup(head_db, stock=5, item_qty=5, requests=1)

    first, second = race(head_db, base, a, a)

    assert first.outcome == "approved"
    assert second.outcome == "refused"
    assert second.reason_code == "not_awaiting_approval"
    assert held(head_db) == 1


def test_a_reject_racing_an_approval_is_refused(head_db):
    base, (a,) = setup(head_db, stock=5, item_qty=5, requests=1)

    first, second = race(head_db, base, a, a, second_decision="reject")

    assert first.outcome == "approved"
    assert second.outcome == "refused"
    assert second.reason_code == "not_awaiting_approval"
    with head_db.engine.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM approval WHERE decision = 'reject'"
        )).scalar_one() == 0


def test_an_approval_sees_an_allocation_committed_while_it_waited(head_db):
    """승인이 재고 잠금을 기다리는 동안 실행이 배분을 커밋해도 낡은 값을 쓰지 않는다.

    첫 트랜잭션은 실행과 같은 순서(지급 항목 -> 재고)로 잠그고 배분한다. 승인이
    지급 항목을 잠그지 않고 읽으면, 재고 잠금을 기다리기 전의 낡은 allocated_qty
    로 판단해서 이미 배분된 수량을 또 예약한다.
    """
    base, (b,) = setup(head_db, stock=5, item_qty=2, requests=1, shared_item=True)
    svc = service()
    ids = {"i": base["item_id"], "m": base["model_id"]}

    def allocate_like_an_execution(c):
        c.execute(text("SELECT 1 FROM assignment_item WHERE id = :i FOR UPDATE"), ids)
        c.execute(text("SELECT 1 FROM asset_stock WHERE asset_model_id = :m "
                       "FOR UPDATE"), ids)
        c.execute(text("UPDATE assignment_item SET allocated_qty = 2 "
                       "WHERE id = :i"), ids)
        c.execute(text("UPDATE asset_stock SET on_hand_qty = on_hand_qty - 2 "
                       "WHERE asset_model_id = :m"), ids)

    _, result = run_blocked(
        head_db, allocate_like_an_execution,
        lambda c: svc.decide_proposal(
            c, actor=actor(base["operator_id"]),
            proposal_id=b["proposal_id"], decision="approve"))

    assert result.outcome == "refused"
    assert result.reason_code == "insufficient_item_quantity"
    assert held(head_db) == 0


# ── 많은 요청이 한꺼번에: 교착 없이 정확한 개수만 통과 ──────────────

def test_many_simultaneous_approvals_reserve_exactly_the_available_stock(head_db):
    requests, stock = 8, 3
    base, proposals = setup(head_db, stock=stock, item_qty=5, requests=requests)
    svc = service()
    barrier = threading.Barrier(requests)
    results, errors = [], []

    def worker(proposal):
        try:
            with head_db.engine.connect() as conn:
                tx = conn.begin()
                barrier.wait(WAIT_SECONDS)
                outcome = svc.decide_proposal(
                    conn, actor=actor(base["operator_id"]),
                    proposal_id=proposal["proposal_id"], decision="approve")
                tx.commit()
                results.append(outcome)
        except Exception as exc:     # noqa: BLE001  교착 등도 여기서 드러난다
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in proposals]
    for t in threads:
        t.start()
    join_all(threads)

    assert not errors, errors
    assert len(results) == requests
    assert sum(r.outcome == "approved" for r in results) == stock
    assert all(r.reason_code == "insufficient_stock"
               for r in results if r.outcome == "refused")
    assert held(head_db) == stock
    with head_db.engine.connect() as c:
        assert c.execute(text(
            "SELECT count(*) FROM request WHERE state = 'needs_review'"
        )).scalar_one() == requests - stock


def test_the_api_also_serializes_simultaneous_approvals(api, head_db):
    requests = 6
    base, proposals = setup(head_db, stock=2, item_qty=5, requests=requests)
    barrier = threading.Barrier(requests)
    statuses, errors = [], []

    def worker(proposal):
        try:
            barrier.wait(WAIT_SECONDS)
            response = api.post(
                f"/proposals/{proposal['proposal_id']}/approval",
                headers={"x-user-id": str(base["operator_id"])},
                json={"proposal_id": proposal["proposal_id"],
                      "decision": "approve"})
            statuses.append(response.status_code)
        except Exception as exc:     # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(p,)) for p in proposals]
    for t in threads:
        t.start()
    join_all(threads)

    assert not errors, errors
    assert sorted(statuses) == [200, 200] + [409] * (requests - 2)
    assert held(head_db) == 2
