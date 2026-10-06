"""승인 + 예약의 동시성 (ADR-002, 2단계).

서비스 함수를 별도 연결 두 개로 직접 호출한다. 첫 트랜잭션이 잠금을 쥔 채
커밋하지 않고, 두 번째가 실제로 잠금에서 막힌 것을 pg_stat_activity 로 확인한
뒤 첫 트랜잭션을 커밋한다. 우연히 겹치기를 기대하지 않으므로 불안정하지 않다.
"""

import importlib
import threading
import time

import pytest
from sqlalchemy import text

from tests.seed import add_item, seed_base, seed_pending

WAIT_SECONDS = 10


def service():
    """서비스 모듈은 구현 전에는 없다. 테스트마다 따로 실패하도록 늦게 가져온다."""
    return importlib.import_module("app.services.approval")


def actor(user_id, *, can_approve=True):
    return {"id": user_id, "kind": "it_operator", "can_approve": can_approve}


def setup(db, *, stock, item_qty, requests, shared_item=False):
    """요청 `requests`개(각 수량 1). shared_item 이 아니면 요청마다 항목을 따로 둔다."""
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        proposals = []
        for _ in range(requests):
            item = base["item_id"] if shared_item else add_item(c, base, qty=item_qty)
            proposals.append(seed_pending(c, base, qty=1, item_id=item))
    return base, proposals


def wait_until_blocked(db, pid):
    """pid 가 다른 트랜잭션이 쥔 행 잠금을 기다릴 때까지 기다린다."""
    deadline = time.monotonic() + WAIT_SECONDS
    with db.engine.connect() as monitor:
        while time.monotonic() < deadline:
            waiting = monitor.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE pid = :pid AND wait_event_type = 'Lock'"
            ), {"pid": pid}).scalar_one()
            if waiting:
                return True
            time.sleep(0.05)
    return False


def race(db, base, first, second, *, first_decision="approve",
         second_decision="approve"):
    """first 가 잠금을 쥔 채 second 가 막힌 것을 확인한 뒤 first 를 커밋한다."""
    svc = service()
    c1 = db.engine.connect()
    tx1 = c1.begin()
    r1 = svc.decide_proposal(c1, actor=actor(base["operator_id"]),
                             proposal_id=first["proposal_id"],
                             decision=first_decision)
    box = {}

    def run_second():
        try:
            c2 = db.engine.connect()
            tx2 = c2.begin()
            box["pid"] = c2.execute(text("SELECT pg_backend_pid()")).scalar_one()
            box["result"] = svc.decide_proposal(
                c2, actor=actor(base["operator_id"]),
                proposal_id=second["proposal_id"], decision=second_decision)
            tx2.commit()
            c2.close()
        except Exception as exc:     # noqa: BLE001  테스트가 검사한다
            box["error"] = exc

    thread = threading.Thread(target=run_second)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while "pid" not in box and time.monotonic() < deadline:
        time.sleep(0.01)
    blocked = "pid" in box and wait_until_blocked(db, box["pid"])
    tx1.commit()
    c1.close()
    thread.join(WAIT_SECONDS)

    assert not thread.is_alive(), "두 번째 트랜잭션이 끝나지 않았다"
    assert "error" not in box, box.get("error")
    assert blocked, "두 번째가 첫 번째의 잠금을 기다리지 않았다 (잠금이 없다)"
    return r1, box["result"]


def test_an_approval_sees_an_allocation_committed_while_it_waited(head_db):
    """승인이 재고 잠금을 기다리는 동안 실행이 배분을 커밋해도 낡은 값을 쓰지 않는다.

    첫 트랜잭션은 실행과 같은 순서(지급 항목 -> 재고)로 잠그고 배분한다. 승인이
    지급 항목을 잠그지 않고 읽으면, 재고 잠금을 기다리기 전의 낡은 allocated_qty
    로 판단해서 이미 배분된 수량을 또 예약한다.
    """
    base, (b,) = setup(head_db, stock=5, item_qty=2, requests=1, shared_item=True)
    svc = service()
    c1 = head_db.engine.connect()
    tx1 = c1.begin()
    ids = {"i": base["item_id"], "m": base["model_id"]}
    c1.execute(text("SELECT 1 FROM assignment_item WHERE id = :i FOR UPDATE"), ids)
    c1.execute(text("SELECT 1 FROM asset_stock WHERE asset_model_id = :m FOR UPDATE"), ids)
    c1.execute(text("UPDATE assignment_item SET allocated_qty = 2 WHERE id = :i"), ids)
    c1.execute(text("UPDATE asset_stock SET on_hand_qty = on_hand_qty - 2 "
                    "WHERE asset_model_id = :m"), ids)
    box = {}

    def approve():
        try:
            c2 = head_db.engine.connect()
            tx2 = c2.begin()
            box["pid"] = c2.execute(text("SELECT pg_backend_pid()")).scalar_one()
            box["result"] = svc.decide_proposal(
                c2, actor=actor(base["operator_id"]),
                proposal_id=b["proposal_id"], decision="approve")
            tx2.commit()
            c2.close()
        except Exception as exc:     # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=approve)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while "pid" not in box and time.monotonic() < deadline:
        time.sleep(0.01)
    blocked = "pid" in box and wait_until_blocked(head_db, box["pid"])
    tx1.commit()
    c1.close()
    thread.join(WAIT_SECONDS)

    assert "error" not in box, box.get("error")
    assert blocked, "승인이 배분 트랜잭션을 기다리지 않았다 (잠금이 없다)"
    assert box["result"].outcome == "refused"
    assert box["result"].reason_code == "insufficient_item_quantity"
    assert held(head_db) == 0


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
    for t in threads:
        t.join(WAIT_SECONDS * 2)

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
    for t in threads:
        t.join(WAIT_SECONDS * 2)

    assert not errors, errors
    assert sorted(statuses) == [200, 200] + [409] * (requests - 2)
    assert held(head_db) == 2
