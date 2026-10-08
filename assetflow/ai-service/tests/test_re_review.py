"""재검토 액션 (ADR-002, 5단계).

needs_review 에 머무는 요청을 같은 처리안으로 다시 awaiting_approval 로 돌려 재승인할 수
있게 한다. 승인도 예약도 만들지 않는다.

확인 순서(요청 잠금 뒤)는 다음과 같고, 이 순서를 아래 테스트가 하나씩 고정한다.
  1. 실행됨          already_executed
  2. 교체됨          proposal_replaced
  3. 이미 대기 중    already_awaiting_approval (1, 2 를 통과한 현재 처리안에만 준다)
     대기 중이 아님  not_in_review
  4. 유효한 승인     approval_still_active
  5. held 예약       reservation_still_held
"""

import json

import pytest
from sqlalchemy import text

from app.services.execution import execute_replacement
from tests.racing import check_invariants
from tests.seed import (
    add_user, approve_pending, revoke, rows, scalar, seed_base, seed_pending,
)
from tests.seed import audit_log_rows as audit
from tests.seed import db_snapshot as snapshot

REASON = "점검을 다시 해서 교체 대상으로 확인됨"


# ── 도우미 ──────────────────────────────────────────────────────────

def approved_case(db, *, stock=5, item_qty=2, qty=1):
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        approved = approve_pending(c, base, seed_pending(c, base, qty=qty))
    return base, approved


def call(api, path, user_id, body=None):
    return api.post(path, headers={"x-user-id": str(user_id)},
                    json={"reason": REASON} if body is None else body)


def approve(api, pid, user_id):
    return api.post(f"/proposals/{pid}/approval",
                    headers={"x-user-id": str(user_id)},
                    json={"proposal_id": pid, "decision": "approve"})


def release(api, pid, user_id):
    return call(api, f"/proposals/{pid}/release", user_id, {"reason": "해제 사유"})


def re_review(api, pid, user_id, body=None):
    return call(api, f"/proposals/{pid}/re-review", user_id, body)


def execute(api, request_id, user_id):
    return api.post("/executions", headers={"x-user-id": str(user_id)},
                    json={"request_id": request_id, "proposal_version": 1})


def assert_refused(response, code):
    assert response.status_code == 409
    body = response.json()
    assert body["outcome"] == "refused"
    assert body["reason_code"] == code
    assert isinstance(body["detail"], str) and body["detail"]


def set_state(db, state):
    with db.engine.begin() as c:
        c.execute(text("UPDATE request SET state = CAST(:s AS request_state)"),
                  {"s": state})


def add_proposal_version(c, pend, version=2):
    """같은 요청에 더 새로운 처리안을 하나 더 만든다(승인 기록은 만들지 않는다)."""
    payload = {"action": "replacement", "assignment_item_id": pend["item_id"],
               "qty": pend["qty"]}
    return c.execute(text(
        "INSERT INTO proposal (request_id, version, action_type, payload, "
        " payload_digest, policy_refs) "
        "VALUES (:r, :v, 'replacement', CAST(:p AS jsonb), :d, '[]') RETURNING id"
    ), {"r": pend["request_id"], "v": version, "p": json.dumps(payload),
        "d": f"d{version}"}).scalar_one()


def executed_case(db, *, variant, state):
    """이미 실행된 처리안. 실행 서비스를 거쳐 정상 실행 상태를 만든 뒤, 요청 상태만 바꾼다.

    실행이 하는 일(예약 소비, 재고 감소, 배분량 증가, 지급 기록, registered)을 테스트가
    따로 흉내 내면 시스템이 도달할 수 없는 상태를 만들 수 있다. 그래서 서비스를 직접
    호출한다. 요청 상태만은 "상태가 어긋나도 실행됨 확인이 먼저인가"를 시험하려고
    테스트 목적에 맞게 바꾼다.

    consumed:         정상 실행 직후. consumed 예약과 그 실행 기록이 있다.
    legacy-execution: 예약 도입 전에 실행된 처리안. 실행 기록과 지급 기록, 재고·배분
                      반영은 있고 예약은 없다(정상 실행에서 예약 행만 뺀다).
    """
    with db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        pend = seed_pending(c, base, qty=1)
        approve_pending(c, base, pend)
        execute_replacement(
            c, actor_id=base["operator_id"], request_id=pend["request_id"],
            proposal_version=1, assignment_item_id=pend["item_id"], qty=1,
            execution_key="k-x", payload_digest=pend["digest"])
        if variant == "legacy-execution":
            c.execute(text("DELETE FROM stock_reservation"))
        c.execute(text("UPDATE request SET state = CAST(:s AS request_state)"),
                  {"s": state})
    # 사전 조건: 실행이 실제로 한 일이 모두 반영된 상태인가
    assert rows(db, "SELECT on_hand_qty FROM asset_stock") == [(4,)]
    assert rows(db, "SELECT allocated_qty FROM assignment_item") == [(1,)]
    assert rows(db, "SELECT count(*) FROM simulated_dispatch") == [(1,)]
    assert rows(db, "SELECT count(*) FROM request_execution") == [(1,)]
    assert rows(db, "SELECT status, execution_key FROM stock_reservation") == (
        [("consumed", "k-x")] if variant == "consumed" else [])
    check_invariants(db)
    return base, pend


# ── 정상 ────────────────────────────────────────────────────────────

def test_a_released_request_returns_to_the_approval_queue(api, head_db):
    base, case = approved_case(head_db)
    op = base["operator_id"]
    release(api, case["proposal_id"], op)
    before = snapshot(head_db)

    response = re_review(api, case["proposal_id"], op)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "re_reviewed"
    assert body["request_id"] == case["request_id"]
    assert rows(head_db, "SELECT state FROM request") == [("awaiting_approval",)]
    # 재검토는 승인도 예약도 만들지 않고, 이력(철회된 승인, 풀린 예약)도 건드리지 않는다
    after = snapshot(head_db)
    assert {k: v for k, v in after.items() if k != "requests"} == \
           {k: v for k, v in before.items() if k != "requests"}

    records = audit(head_db)
    assert [r["action"] for r in records] == ["release", "re_review"]
    assert records[1]["target_type"] == "proposal"
    assert records[1]["target_id"] == str(case["proposal_id"])
    assert records[1]["result"] == "allowed"
    assert REASON in records[1]["reason"]


def test_a_request_refused_for_quantity_can_be_re_reviewed(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=1, item_qty=5)
        approve_pending(c, base, seed_pending(c, base, qty=1))      # 재고 1개를 A 가 잡음
        b = seed_pending(c, base, qty=1)
    op = base["operator_id"]
    assert approve(api, b["proposal_id"], op).status_code == 409
    assert rows(head_db, "SELECT state FROM request WHERE id = :r",
                r=b["request_id"]) == [("needs_review",)]

    response = re_review(api, b["proposal_id"], op)

    assert response.status_code == 200
    assert rows(head_db, "SELECT state FROM request WHERE id = :r",
                r=b["request_id"]) == [("awaiting_approval",)]


def test_approve_release_re_review_approve_and_execute(api, head_db):
    """한 요청이 승인 -> 해제 -> 재검토 -> 재승인 -> 실행까지 가는 전체 흐름."""
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        pend = seed_pending(c, base, qty=1)
    op, pid, rid = base["operator_id"], pend["proposal_id"], pend["request_id"]

    assert approve(api, pid, op).status_code == 200
    assert release(api, pid, op).status_code == 200
    assert re_review(api, pid, op).status_code == 200
    again = approve(api, pid, op)
    assert again.status_code == 200
    assert execute(api, rid, op).status_code == 200

    # 옛 승인은 철회로, 옛 예약은 해제로 남고 새 승인과 새 예약이 실행에 쓰였다
    assert rows(head_db, "SELECT revoked_at IS NOT NULL FROM approval ORDER BY id") == \
        [(True,), (False,)]
    assert rows(head_db, "SELECT status FROM stock_reservation ORDER BY id") == \
        [("released",), ("consumed",)]
    assert rows(head_db, "SELECT on_hand_qty FROM asset_stock") == [(4,)]
    assert rows(head_db, "SELECT allocated_qty FROM assignment_item") == [(1,)]
    assert rows(head_db, "SELECT state FROM request") == [("registered",)]
    assert rows(head_db, "SELECT count(*) FROM request_execution") == [(1,)]


def test_a_freed_quantity_lets_the_refused_request_through_after_re_review(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=1, item_qty=5)
        a = approve_pending(c, base, seed_pending(c, base, qty=1))
        b = seed_pending(c, base, qty=1)
    op = base["operator_id"]
    assert approve(api, b["proposal_id"], op).status_code == 409     # A 가 재고를 잡고 있다

    assert release(api, a["proposal_id"], op).status_code == 200      # A 해제
    assert re_review(api, b["proposal_id"], op).status_code == 200    # B 다시 대기
    assert approve(api, b["proposal_id"], op).status_code == 200      # 이제 승인된다


# ── 멱등 ────────────────────────────────────────────────────────────

def test_a_second_re_review_changes_nothing(api, head_db):
    base, case = approved_case(head_db)
    op = base["operator_id"]
    release(api, case["proposal_id"], op)
    assert re_review(api, case["proposal_id"], op).status_code == 200
    before, audits = snapshot(head_db), audit(head_db)

    response = re_review(api, case["proposal_id"], op, {"reason": "한 번 더"})

    assert response.status_code == 200
    assert response.json()["outcome"] == "already_awaiting_approval"
    assert snapshot(head_db) == before
    assert audit(head_db) == audits


# ── 확인 순서 1: 실행됨이 가장 먼저 ─────────────────────────────────

@pytest.mark.parametrize("variant", ["legacy-execution", "consumed"])
@pytest.mark.parametrize("state", ["needs_review", "awaiting_approval", "registered"])
def test_an_executed_proposal_is_refused_whatever_the_state(api, head_db, variant, state):
    """실행 기록이나 consumed 예약이 있으면 요청 상태와 무관하게 되돌릴 수 없다.

    특히 이미 awaiting_approval 이어도 already_awaiting_approval 이 아니고,
    registered 여도 not_in_review 가 아니라 already_executed 가 맞다.
    """
    base, pend = executed_case(head_db, variant=variant, state=state)
    before = snapshot(head_db)

    response = re_review(api, pend["proposal_id"], base["operator_id"])

    assert_refused(response, "already_executed")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("already_executed")


# ── 확인 순서 2: 교체됨이 그다음 ────────────────────────────────────

def test_an_old_proposal_is_replaced_even_if_the_request_is_awaiting_approval(api, head_db):
    """오래된 처리안은 요청이 이미 awaiting_approval 이어도 proposal_replaced 다.
    같은 요청의 현재(최신) 처리안만 already_awaiting_approval 로 답한다."""
    with head_db.engine.begin() as c:
        base = seed_base(c)
        v1 = seed_pending(c, base, qty=1)            # 요청 상태는 awaiting_approval
        v2 = add_proposal_version(c, v1)
    op = base["operator_id"]
    before = snapshot(head_db)

    old = re_review(api, v1["proposal_id"], op)

    assert_refused(old, "proposal_replaced")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("proposal_replaced")

    current = re_review(api, v2, op)

    assert current.status_code == 200
    assert current.json()["outcome"] == "already_awaiting_approval"


@pytest.mark.parametrize("state", ["ready_to_execute", "needs_review", "rejected"])
def test_replacement_is_checked_before_the_request_state(api, head_db, state):
    with head_db.engine.begin() as c:
        base = seed_base(c)
        v1 = seed_pending(c, base, qty=1)
        add_proposal_version(c, v1)
    set_state(head_db, state)

    response = re_review(api, v1["proposal_id"], base["operator_id"])

    assert_refused(response, "proposal_replaced")


def test_execution_is_checked_before_replacement(api, head_db):
    """실행된 처리안이 교체까지 됐다면 이유는 already_executed 다."""
    base, pend = executed_case(head_db, variant="legacy-execution",
                               state="needs_review")
    with head_db.engine.begin() as c:
        add_proposal_version(c, pend)

    response = re_review(api, pend["proposal_id"], base["operator_id"])

    assert_refused(response, "already_executed")


# ── 확인 순서 3~5 ───────────────────────────────────────────────────

@pytest.mark.parametrize("state", ["ready_to_execute", "rejected", "outcome_unknown"])
def test_only_a_request_in_review_can_return(api, head_db, state):
    """실행되지 않은 처리안의 요청이 needs_review 도 awaiting_approval 도 아닌 경우.

    처리안이 있는 요청이 실제로 가질 수 있는 상태만 쓴다. registered 는 항상 실행
    기록이 있으므로 already_executed 테스트에서 다루고, needs_information·escalated
    는 처리안이 만들어지기 전에 닫히는 상태라 여기서는 만들 수 없다."""
    with head_db.engine.begin() as c:
        base = seed_base(c)
        pend = seed_pending(c, base, qty=1)
    set_state(head_db, state)
    before = snapshot(head_db)

    response = re_review(api, pend["proposal_id"], base["operator_id"])

    assert_refused(response, "not_in_review")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("not_in_review")


def test_a_still_active_approval_blocks_the_return(api, head_db):
    base, case = approved_case(head_db)           # 유효한 승인과 held 예약이 있다
    set_state(head_db, "needs_review")            # 어긋난 상태
    before = snapshot(head_db)

    response = re_review(api, case["proposal_id"], base["operator_id"])

    # 승인과 예약이 모두 남아 있으면 승인 쪽 사유가 먼저다
    assert_refused(response, "approval_still_active")
    assert snapshot(head_db) == before


def test_a_still_held_reservation_blocks_the_return(api, head_db):
    base, case = approved_case(head_db)
    with head_db.engine.begin() as c:
        revoke(c, base, case["approval_id"])      # 승인은 철회됐지만 예약이 held 로 남음
    set_state(head_db, "needs_review")
    before = snapshot(head_db)

    response = re_review(api, case["proposal_id"], base["operator_id"])

    assert_refused(response, "reservation_still_held")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("reservation_still_held")


# ── 권한과 입력 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("who", ["operator-without-right", "employee"])
def test_missing_permission_is_refused_and_audited(api, head_db, who):
    base, case = approved_case(head_db)
    release(api, case["proposal_id"], base["operator_id"])
    with head_db.engine.begin() as c:
        user = (add_user(c, "op-no", kind="it_operator", can_approve=False)
                if who == "operator-without-right" else base["employee_id"])
    before, audits = snapshot(head_db), audit(head_db)

    response = re_review(api, case["proposal_id"], user)

    assert response.status_code == 403
    assert response.json()["reason_code"] == "no_permission"
    assert snapshot(head_db) == before
    new_records = audit(head_db)[len(audits):]
    assert len(new_records) == 1 and new_records[0]["result"] == "denied"
    assert new_records[0]["reason"].startswith("no_permission")


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}],
                         ids=["missing", "empty", "blank"])
def test_a_reason_is_required_and_not_audited_when_missing(api, head_db, body):
    base, case = approved_case(head_db)
    release(api, case["proposal_id"], base["operator_id"])
    before, audits = snapshot(head_db), audit(head_db)

    response = re_review(api, case["proposal_id"], base["operator_id"], body)

    assert response.status_code == 400
    assert snapshot(head_db) == before
    assert audit(head_db) == audits


def test_an_unknown_proposal_is_a_plain_404(api, head_db):
    base, _ = approved_case(head_db)

    response = re_review(api, 999999, base["operator_id"])

    assert response.status_code == 404
    # 라우트가 없을 때의 404 는 detail 이 "Not Found" 다. 구현이 처리안을 찾지 못해서
    # 낸 404 인지 detail 로 구분한다 (승인·해제 API 와 같은 문구).
    assert response.json() == {"detail": "처리안을 찾을 수 없다"}
    assert audit(head_db) == []


# ── 원자성 ──────────────────────────────────────────────────────────

def test_a_failure_while_auditing_rolls_the_state_change_back(api, head_db):
    base, case = approved_case(head_db)
    release(api, case["proposal_id"], base["operator_id"])
    with head_db.engine.begin() as c:
        # 감사 기록 삽입을 일부러 실패시킨다. 상태 전환은 이미 실행된 뒤다.
        c.execute(text("ALTER TABLE audit_log "
                       "ADD CONSTRAINT boom CHECK (action <> 're_review') NOT VALID"))
    before = snapshot(head_db)

    response = re_review(api, case["proposal_id"], base["operator_id"])

    assert response.status_code == 500
    assert snapshot(head_db) == before
    assert rows(head_db, "SELECT state FROM request") == [("needs_review",)]
