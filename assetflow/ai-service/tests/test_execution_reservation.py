"""실행이 예약을 소비한다 (ADR-002, 3단계).

실행은 새 재고를 두고 경쟁하지 않는다. 승인 때 만든 held 예약을 consumed 로
바꾸면서 재고와 배분량을 반영한다. 서비스 수준 테스트는 공유 `conn`(롤백)을
쓰고, 커밋 의미와 끝에서 끝까지는 `api` + `head_db`로 확인한다.
"""

import pytest
from sqlalchemy import text

from app.services.execution import ExecutionRejected, execute_replacement
from tests.seed import (
    add_item, approve_pending, insert_reservation, revoke, rows, seed_base,
    seed_pending,
)


# ── 도우미 ──────────────────────────────────────────────────────────

def run(conn, case, **over):
    args = dict(
        actor_id=case["operator_id"], request_id=case["request_id"],
        proposal_version=1, assignment_item_id=case["item_id"], qty=1,
        execution_key="k-1", payload_digest="d1")
    args.update(over)
    return execute_replacement(conn, **args)


def snap(conn):
    """업무 상태 전체. 거부 뒤 "아무것도 안 바뀌었다"를 비교하는 데 쓴다."""
    def q(sql):
        return [tuple(r) for r in conn.execute(text(sql))]
    return {
        "reservations": q("SELECT id, status, execution_key "
                          "FROM stock_reservation ORDER BY id"),
        "on_hand": q("SELECT asset_model_id, on_hand_qty FROM asset_stock ORDER BY 1"),
        "allocated": q("SELECT id, allocated_qty FROM assignment_item ORDER BY id"),
        "executions": q("SELECT execution_key FROM request_execution"),
        "dispatches": q("SELECT execution_key, qty FROM simulated_dispatch"),
        "requests": q("SELECT id, state FROM request ORDER BY id"),
    }


def one(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


# ── 정상 ────────────────────────────────────────────────────────────

def test_execution_consumes_the_reservation_and_applies_it(conn, replacement_case):
    run(conn, replacement_case)

    reservation = conn.execute(text(
        "SELECT status, execution_key, resolved_at IS NOT NULL "
        "FROM stock_reservation")).one()
    assert tuple(reservation) == ("consumed", "k-1", True)
    assert one(conn, "SELECT on_hand_qty FROM asset_stock") == 4
    assert one(conn, "SELECT allocated_qty FROM assignment_item "
                     "WHERE id = :i", i=replacement_case["item_id"]) == 1
    assert [tuple(r) for r in conn.execute(text(
        "SELECT execution_key, qty FROM simulated_dispatch"))] == [("k-1", 1)]
    assert one(conn, "SELECT count(*) FROM request_execution") == 1
    assert one(conn, "SELECT state FROM request") == "registered"


def test_a_consumed_reservation_no_longer_holds_quantity(conn, replacement_case):
    run(conn, replacement_case)

    assert one(conn, "SELECT count(*) FROM stock_reservation "
                     "WHERE status = 'held'") == 0


@pytest.mark.parametrize("state", ["ready_to_execute", "outcome_unknown"])
def test_the_states_that_may_execute(conn, replacement_case, state):
    conn.execute(text("UPDATE request SET state = CAST(:s AS request_state)"),
                 {"s": state})

    run(conn, replacement_case)

    assert one(conn, "SELECT state FROM request") == "registered"


# ── 거부: 아무것도 바뀌지 않는다 ────────────────────────────────────

def _other_item(conn, case):
    return {"assignment_item_id": add_item(conn, case["base"], qty=2)}


REFUSALS = [
    ("no-reservation",
     lambda c, case: c.execute(text("DELETE FROM stock_reservation")) and {},
     "예약"),
    ("released-reservation",
     lambda c, case: c.execute(text(
         "UPDATE stock_reservation SET status = 'released', "
         "resolved_at = now()")) and {},
     "예약"),
    ("other-item-than-reserved", _other_item, "예약"),
    ("other-quantity-than-reserved", lambda c, case: {"qty": 2}, "예약"),
    # 실행 시점 재고 부족: 승인 뒤 실제 보유량이 예약보다 적어졌다
    ("stock-fell-below-the-reservation",
     lambda c, case: c.execute(text("UPDATE asset_stock SET on_hand_qty = 0"))
     and {}, "보유"),
    ("remaining-item-quantity-fell",
     lambda c, case: c.execute(text("UPDATE assignment_item SET allocated_qty = 2"))
     and {}, "미처리 수량"),
    ("revoked-approval",
     lambda c, case: revoke(c, case["base"], case["approval_id"]) or {},
     "승인이 없다"),
    ("not-a-replacement",
     lambda c, case: c.execute(text(
         "UPDATE proposal SET action_type = 'cost_claim_draft'")) and {},
     "교체"),
]


@pytest.mark.parametrize("label, prepare, pattern", REFUSALS,
                         ids=[r[0] for r in REFUSALS])
def test_a_refused_execution_changes_nothing(conn, replacement_case,
                                             label, prepare, pattern):
    overrides = prepare(conn, replacement_case) or {}
    before = snap(conn)

    with pytest.raises(ExecutionRejected, match=pattern):
        run(conn, replacement_case, **overrides)

    assert snap(conn) == before
    # 거부된 실행의 키는 저장되지 않는다. 사정이 풀리면 같은 키로 다시 시도할 수 있다.
    assert one(conn, "SELECT count(*) FROM request_execution") == 0


@pytest.mark.parametrize("state", ["awaiting_approval", "needs_review",
                                   "rejected", "needs_information", "escalated"])
def test_only_a_request_ready_to_execute_may_run(conn, replacement_case, state):
    conn.execute(text("UPDATE request SET state = CAST(:s AS request_state)"),
                 {"s": state})
    before = snap(conn)

    with pytest.raises(ExecutionRejected, match="상태"):
        run(conn, replacement_case)

    assert snap(conn) == before


def test_an_already_consumed_reservation_cannot_be_used_again(conn, replacement_case):
    run(conn, replacement_case, execution_key="k-0")
    conn.execute(text("UPDATE request SET state = 'ready_to_execute'"))
    before = snap(conn)

    with pytest.raises(ExecutionRejected, match="예약"):
        run(conn, replacement_case, execution_key="k-2")

    assert snap(conn) == before


# ── 재시도 ──────────────────────────────────────────────────────────

def test_a_retry_with_the_same_key_is_answered_before_the_state_guard(
        conn, replacement_case):
    """첫 실행 뒤 요청은 registered 라서 상태 가드에는 걸린다. 같은 키 재시도는
    그보다 먼저 확인해서 기존 결과를 돌려줘야 한다."""
    run(conn, replacement_case)
    assert one(conn, "SELECT state FROM request") == "registered"
    before = snap(conn)

    assert run(conn, replacement_case) == "k-1"

    assert snap(conn) == before


# ── 철회된 승인과 새 승인이 공존할 때 ───────────────────────────────

def test_a_revoked_approval_next_to_a_new_one_does_not_break_execution(
        conn, replacement_case):
    case = replacement_case
    base = case["base"]
    revoke(conn, base, case["approval_id"])
    conn.execute(text("UPDATE stock_reservation SET status = 'released', "
                      "resolved_at = now()"))
    new_approval = conn.execute(text(
        "INSERT INTO approval (request_id, proposal_id, proposal_version, "
        " payload_digest, approver_id, decision) "
        "VALUES (:r, :p, 1, 'd1', :o, 'approve') RETURNING id"
    ), {"r": case["request_id"], "p": case["proposal_id"],
        "o": base["operator_id"]}).scalar_one()
    new_reservation = insert_reservation(
        conn, base,
        {"request_id": case["request_id"], "proposal_id": case["proposal_id"],
         "version": 1, "digest": "d1", "approval_id": new_approval,
         "execution_key": None})

    run(conn, case)

    assert one(conn, "SELECT status FROM stock_reservation WHERE id = :i",
               i=new_reservation) == "consumed"
    assert one(conn, "SELECT state FROM request") == "registered"


# ── 커밋 의미: 실제 get_conn 으로 ───────────────────────────────────

def _approved(db, *, stock=5, item_qty=2, qty=1):
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        approved = approve_pending(c, base, seed_pending(c, base, qty=qty))
    return base, approved


def _db_state(db):
    return {
        "reservations": rows(db, "SELECT status, execution_key FROM stock_reservation"),
        "on_hand": rows(db, "SELECT on_hand_qty FROM asset_stock"),
        "allocated": rows(db, "SELECT allocated_qty FROM assignment_item"),
        "executions": rows(db, "SELECT execution_key FROM request_execution"),
        "dispatches": rows(db, "SELECT 1 FROM simulated_dispatch"),
        "requests": rows(db, "SELECT state FROM request"),
    }


def test_a_failure_while_consuming_rolls_everything_back(api, head_db):
    base, approved = _approved(head_db)
    with head_db.engine.begin() as c:
        # 마지막 단계(지급 기록)를 일부러 실패시킨다. NOT VALID 여도 새 행에는 적용된다.
        c.execute(text("ALTER TABLE simulated_dispatch "
                       "ADD CONSTRAINT boom CHECK (false) NOT VALID"))
    before = _db_state(head_db)

    response = api.post(
        "/executions", headers={"x-user-id": str(base["operator_id"])},
        json={"request_id": approved["request_id"], "proposal_version": 1})

    assert response.status_code == 500
    assert _db_state(head_db) == before
    assert before["reservations"] == [("held", None)]


def test_approve_then_execute_through_the_api(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        pending = seed_pending(c, base, qty=1)
    op = {"x-user-id": str(base["operator_id"])}

    approval = api.post(f"/proposals/{pending['proposal_id']}/approval", headers=op,
                        json={"proposal_id": pending["proposal_id"],
                              "decision": "approve"})
    assert approval.status_code == 200
    # 승인은 예약만 한다. 재고와 배분량은 실행이 바꾼다.
    assert rows(head_db, "SELECT on_hand_qty FROM asset_stock") == [(5,)]
    assert rows(head_db, "SELECT allocated_qty FROM assignment_item") == [(0,)]

    execution = api.post("/executions", headers=op,
                         json={"request_id": pending["request_id"],
                               "proposal_version": 1})

    key = f"{pending['request_id']}:1:replacement"
    assert execution.status_code == 200
    assert execution.json() == {"execution_key": key, "outcome": "registered"}
    assert rows(head_db, "SELECT status, execution_key FROM stock_reservation") == [
        ("consumed", key)]
    assert rows(head_db, "SELECT on_hand_qty FROM asset_stock") == [(4,)]
    assert rows(head_db, "SELECT allocated_qty FROM assignment_item") == [(1,)]
    assert rows(head_db, "SELECT qty FROM simulated_dispatch") == [(1,)]
    assert rows(head_db, "SELECT state FROM request") == [("registered",)]
    assert rows(head_db, "SELECT count(*) FROM approval") == [(1,)]
