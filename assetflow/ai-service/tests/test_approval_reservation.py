"""승인 + 재고 예약 (ADR-002, 2단계).

`api` fixture 는 실제 get_conn(성공하면 커밋, 예외면 롤백)으로 도는 클라이언트다.
그래서 "거절해도 감사 기록이 남는가" 같은 커밋 의미를 운영과 똑같이 확인한다.
결과는 새 연결로 읽어 커밋된 것만 본다.
"""

import json

import pytest
from sqlalchemy import text

from tests.seed import (
    add_assignment, add_item, add_user, insert_reservation, revoke, rows, scalar,
    seed_base, seed_pending, seed_request,
)

# ── 도우미 ──────────────────────────────────────────────────────────


def snapshot(db):
    """업무 상태 전체. 거절 뒤 "아무것도 안 바뀌었다"를 비교하는 데 쓴다."""
    return {
        "approvals": rows(db, "SELECT id, request_id, decision FROM approval ORDER BY id"),
        "reservations": rows(
            db, "SELECT id, request_id, status, qty FROM stock_reservation ORDER BY id"),
        "requests": rows(db, "SELECT id, state FROM request ORDER BY id"),
        "stock": rows(db, "SELECT asset_model_id, on_hand_qty FROM asset_stock ORDER BY 1"),
        "items": rows(db, "SELECT id, allocated_qty FROM assignment_item ORDER BY id"),
    }


def audit(db):
    return [dict(zip(("action", "target_type", "target_id", "result", "reason"), r))
            for r in rows(
                db, "SELECT action, target_type, target_id, result, reason "
                    "FROM audit_log ORDER BY id")]


def world(db, *, stock=5, item_qty=2):
    with db.engine.begin() as c:
        return seed_base(c, stock=stock, item_qty=item_qty)


def pending(db, base, **kw):
    with db.engine.begin() as c:
        return seed_pending(c, base, **kw)


def decide(api, proposal_id, user_id, decision="approve", **body):
    return api.post(
        f"/proposals/{proposal_id}/approval",
        headers={"x-user-id": str(user_id)},
        json={"proposal_id": proposal_id, "decision": decision, **body},
    )


def assert_refused(response, status, reason_code):
    assert response.status_code == status
    body = response.json()
    assert body["outcome"] == "refused"
    assert body["reason_code"] == reason_code
    assert isinstance(body["detail"], str) and body["detail"]


# ── 정상 승인 ───────────────────────────────────────────────────────

def test_approval_creates_the_approval_and_a_held_reservation(api, head_db):
    base = world(head_db)
    req = pending(head_db, base, qty=2)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "approved"
    assert body["request_id"] == req["request_id"]
    assert body["decision"] == "approve"
    assert body["approver_id"] == base["operator_id"]

    approval = rows(head_db, "SELECT id, approver_id, decision FROM approval")
    assert approval == [(approval[0][0], base["operator_id"], "approve")]

    reservation = rows(
        head_db,
        "SELECT id, request_id, proposal_id, proposal_version, payload_digest, "
        "       approval_id, assignment_item_id, asset_model_id, qty, status "
        "FROM stock_reservation")
    assert reservation == [(
        body["reservation_id"], req["request_id"], req["proposal_id"], 1, "d1",
        approval[0][0], base["item_id"], base["model_id"], 2, "held")]

    assert scalar(head_db, "SELECT state FROM request") == "ready_to_execute"


def test_approval_does_not_touch_stock_or_allocation(api, head_db):
    base = world(head_db)
    req = pending(head_db, base, qty=2)

    decide(api, req["proposal_id"], base["operator_id"])

    assert scalar(head_db, "SELECT on_hand_qty FROM asset_stock") == 5
    assert scalar(head_db, "SELECT allocated_qty FROM assignment_item") == 0


def test_approver_comes_from_the_server_not_the_body(api, head_db):
    base = world(head_db)
    req = pending(head_db, base)

    response = decide(api, req["proposal_id"], base["operator_id"], approver_id=999)

    assert response.status_code == 200
    assert scalar(head_db, "SELECT approver_id FROM approval") == base["operator_id"]


# ── 수량 부족: 승인을 만들지 않고 검토로 보낸다 ─────────────────────

@pytest.mark.parametrize("stock, item_qty, qty, reason", [
    (1, 5, 2, "insufficient_stock"),
    (10, 1, 2, "insufficient_item_quantity"),
    (1, 1, 2, "insufficient_item_quantity"),   # 둘 다 모자라면 지급 항목 사유가 먼저
], ids=["stock", "item", "both-item-first"])
def test_insufficient_quantity_creates_no_approval_and_moves_to_review(
        api, head_db, stock, item_qty, qty, reason):
    base = world(head_db, stock=stock, item_qty=item_qty)
    req = pending(head_db, base, qty=qty)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert_refused(response, 409, reason)
    assert rows(head_db, "SELECT 1 FROM approval") == []
    assert rows(head_db, "SELECT 1 FROM stock_reservation") == []
    assert scalar(head_db, "SELECT state FROM request") == "needs_review"
    assert scalar(head_db, "SELECT on_hand_qty FROM asset_stock") == stock
    assert scalar(head_db, "SELECT allocated_qty FROM assignment_item") == 0


def test_insufficient_quantity_keeps_its_audit_record(api, head_db):
    """needs_review 전환과 감사 기록이 함께 커밋돼 새 연결에서 보인다."""
    base = world(head_db, stock=1, item_qty=5)
    req = pending(head_db, base, qty=2)

    decide(api, req["proposal_id"], base["operator_id"])

    records = audit(head_db)
    assert len(records) == 1
    assert records[0]["action"] == "approve"
    assert records[0]["target_type"] == "proposal"
    assert records[0]["target_id"] == str(req["proposal_id"])
    assert records[0]["result"] == "denied"
    assert records[0]["reason"].startswith("insufficient_stock")


def test_reservable_stock_excludes_quantity_held_for_other_requests(api, head_db):
    base = world(head_db, stock=5)
    with head_db.engine.begin() as c:
        items = [add_item(c, base, qty=10) for _ in range(3)]
    first = pending(head_db, base, qty=2, item_id=items[0])
    second = pending(head_db, base, qty=4, item_id=items[1])
    third = pending(head_db, base, qty=3, item_id=items[2])
    op = base["operator_id"]

    assert decide(api, first["proposal_id"], op).status_code == 200   # 보유 5, 예약 2
    assert_refused(decide(api, second["proposal_id"], op), 409,
                   "insufficient_stock")                               # 가능 3 < 4
    assert decide(api, third["proposal_id"], op).status_code == 200   # 가능 3 >= 3


def test_requests_sharing_one_assignment_item_cannot_overbook_it(api, head_db):
    base = world(head_db, stock=10, item_qty=2)
    a = pending(head_db, base, qty=2)
    b = pending(head_db, base, qty=1)
    op = base["operator_id"]

    assert decide(api, a["proposal_id"], op).status_code == 200
    assert_refused(decide(api, b["proposal_id"], op), 409,
                   "insufficient_item_quantity")


# (상태, 처음 재고, 처음 지급 항목 수량, 시나리오). 새 요청은 2개를 원한다.
# consumed 는 실행이 끝난 예약이라 재고 2개와 지급량 2개가 이미 반영된 상태를
# 만든다(재고 4 -> 2, allocated 0 -> 2). released 는 풀렸으니 아무것도 안 바뀐다.
HELD_ONLY_CASES = [
    ("consumed", 10, 4, "item-is-tight"),    # 반영 후 남은 지급 수량 4-2 = 2
    ("consumed", 4, 10, "stock-is-tight"),   # 반영 후 보유 재고 4-2 = 2
    ("released", 10, 2, "item-is-tight"),    # 남은 지급 수량 2
    ("released", 2, 10, "stock-is-tight"),   # 보유 재고 2
]


@pytest.mark.parametrize("status, stock, item_qty, scenario", HELD_ONLY_CASES,
                         ids=[f"{c[0]}-{c[3]}" for c in HELD_ONLY_CASES])
def test_only_held_reservations_reduce_what_is_reservable(
        api, head_db, status, stock, item_qty, scenario):
    """held 만 예약 가능량을 줄인다.

    consumed 를 held 처럼 또 세면 이미 반영된 2개를 한 번 더 빼서 새 요청(2개)이
    거절된다. 한쪽 한도만 빠듯하게 만들어서 어느 합계가 틀려도 드러나게 한다.
    """
    base = world(head_db, stock=stock, item_qty=item_qty)
    with head_db.engine.begin() as c:
        old = seed_request(c, base, state="registered", execution_key="k-old")
        insert_reservation(c, base, old, qty=2, status=status)
        if status == "consumed":
            # 실행이 한 일: 지급량 증가와 재고 감소. 예약만 있고 이것이 없는
            # 상태는 실제로 생길 수 없다.
            c.execute(text("UPDATE assignment_item SET allocated_qty = allocated_qty + 2 "
                           "WHERE id = :i"), {"i": base["item_id"]})
            c.execute(text("UPDATE asset_stock SET on_hand_qty = on_hand_qty - 2 "
                           "WHERE asset_model_id = :m"), {"m": base["model_id"]})
    req = pending(head_db, base, qty=2)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert response.status_code == 200


# ── 상태 가드와 중복 ────────────────────────────────────────────────

def test_a_second_approval_is_refused_and_changes_nothing(api, head_db):
    base = world(head_db)
    req = pending(head_db, base, qty=1)
    op = base["operator_id"]
    assert decide(api, req["proposal_id"], op).status_code == 200
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], op)

    assert_refused(response, 409, "not_awaiting_approval")
    assert snapshot(head_db) == before
    records = audit(head_db)
    assert len(records) == 1 and records[0]["result"] == "denied"
    assert records[0]["reason"].startswith("not_awaiting_approval")


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("state", [
    "needs_information", "needs_review", "rejected", "ready_to_execute",
    "outcome_unknown", "registered", "escalated"])
def test_only_an_awaiting_request_can_be_decided(api, head_db, state, decision):
    base = world(head_db)
    req = pending(head_db, base, state=state)
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"], decision)

    assert_refused(response, 409, "not_awaiting_approval")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("not_awaiting_approval")


def test_an_existing_active_approval_blocks_a_new_one(api, head_db):
    """상태가 어긋난 데이터(대기 상태인데 활성 승인 있음)에서도 중복 예약이 안 생긴다."""
    base = world(head_db)
    req = pending(head_db, base, qty=1)
    with head_db.engine.begin() as c:
        c.execute(text(
            "INSERT INTO approval (request_id, proposal_id, proposal_version, "
            " payload_digest, approver_id, decision) "
            "VALUES (:r, :p, 1, 'd1', :o, 'approve')"
        ), {"r": req["request_id"], "p": req["proposal_id"],
            "o": base["operator_id"]})
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "already_approved")
    assert snapshot(head_db) == before


# ── 어긋난 데이터: 승인은 없는데 살아 있는 예약이 남은 경우 ──────────

@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("status", ["held", "consumed"])
def test_a_live_reservation_blocks_any_new_decision(
        api, head_db, status, decision):
    """대기 상태인데 승인은 철회됐고 예약만 살아 있다. 500 이나 고아 예약이 되면 안 된다."""
    base = world(head_db)
    req = pending(head_db, base, qty=1)
    with head_db.engine.begin() as c:
        approval_id = c.execute(text(
            "INSERT INTO approval (request_id, proposal_id, proposal_version, "
            " payload_digest, approver_id, decision) "
            "VALUES (:r, :p, 1, 'd1', :o, 'approve') RETURNING id"
        ), {"r": req["request_id"], "p": req["proposal_id"],
            "o": base["operator_id"]}).scalar_one()
        revoke(c, base, approval_id)
        if status == "consumed":
            c.execute(text(
                "INSERT INTO request_execution (execution_key, request_id, "
                " proposal_version, action_type, payload_digest, outcome) "
                "VALUES ('k-live', :r, 1, 'replacement', 'd1', 'registered')"
            ), {"r": req["request_id"]})
        insert_reservation(
            c, base,
            {"request_id": req["request_id"], "proposal_id": req["proposal_id"],
             "version": 1, "digest": "d1", "approval_id": approval_id,
             "execution_key": "k-live"},
            status=status)
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"], decision)

    assert_refused(response, 409, "already_reserved")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("already_reserved")


# ── 교체 처리안만 예약한다 ───────────────────────────────────────────

@pytest.mark.parametrize("action_type", ["cost_claim_draft", "escalate"])
def test_only_a_replacement_proposal_can_be_approved(api, head_db, action_type):
    base = world(head_db)
    req = pending(head_db, base, action_type=action_type)
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "invalid_proposal")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("invalid_proposal")


@pytest.mark.parametrize("action", ["cost_claim_draft", "escalate", None])
def test_the_payload_must_also_say_replacement(api, head_db, action):
    base = world(head_db)
    payload = {"assignment_item_id": base["item_id"], "qty": 1}
    if action is not None:
        payload["action"] = action
    req = pending(head_db, base, payload=payload)
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "invalid_proposal")
    assert snapshot(head_db) == before


def test_a_proposal_of_another_kind_can_still_be_rejected(api, head_db):
    """거절은 예약과 무관하다. 종류를 따지지 않는다."""
    base = world(head_db)
    req = pending(head_db, base, action_type="cost_claim_draft")

    response = decide(api, req["proposal_id"], base["operator_id"], "reject")

    assert response.status_code == 200
    assert response.json()["outcome"] == "rejected"
    assert rows(head_db, "SELECT 1 FROM stock_reservation") == []


# ── 거절 ────────────────────────────────────────────────────────────

def test_reject_records_the_decision_without_a_reservation(api, head_db):
    base = world(head_db)
    req = pending(head_db, base, qty=2)

    response = decide(api, req["proposal_id"], base["operator_id"], "reject")

    assert response.status_code == 200
    assert response.json()["outcome"] == "rejected"
    assert rows(head_db, "SELECT decision FROM approval") == [("reject",)]
    assert rows(head_db, "SELECT 1 FROM stock_reservation") == []
    assert scalar(head_db, "SELECT state FROM request") == "rejected"
    assert scalar(head_db, "SELECT on_hand_qty FROM asset_stock") == 5


# ── 권한 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("who", ["operator-without-right", "employee"])
def test_missing_permission_is_refused_and_audited(api, head_db, who):
    base = world(head_db)
    req = pending(head_db, base)
    with head_db.engine.begin() as c:
        user = (add_user(c, "op-no", kind="it_operator", can_approve=False)
                if who == "operator-without-right" else base["employee_id"])
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], user)

    assert_refused(response, 403, "no_permission")
    assert snapshot(head_db) == before
    records = audit(head_db)
    assert len(records) == 1 and records[0]["result"] == "denied"
    assert records[0]["reason"].startswith("no_permission")


# ── 입력 오류는 기록하지 않는다 ─────────────────────────────────────

@pytest.mark.parametrize("decision", ["maybe", "", "APPROVE"])
def test_invalid_decision_is_a_plain_400_and_not_audited(api, head_db, decision):
    base = world(head_db)
    req = pending(head_db, base)
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"], decision)

    assert response.status_code == 400
    assert snapshot(head_db) == before
    assert audit(head_db) == []


def test_unknown_proposal_is_a_plain_404_and_not_audited(api, head_db):
    base = world(head_db)

    response = decide(api, 999999, base["operator_id"])

    assert response.status_code == 404
    assert audit(head_db) == []


def test_body_and_path_must_name_the_same_proposal(api, head_db):
    base = world(head_db)
    req = pending(head_db, base)
    before = snapshot(head_db)

    response = api.post(
        f"/proposals/{req['proposal_id']}/approval",
        headers={"x-user-id": str(base["operator_id"])},
        json={"proposal_id": req["proposal_id"] + 1, "decision": "approve"})

    assert response.status_code == 400
    assert snapshot(head_db) == before
    assert audit(head_db) == []


# ── 잘못된 처리안은 예약하기 전에 막는다 ────────────────────────────

@pytest.mark.parametrize("payload", [
    {},
    {"action": "replacement", "qty": 1},
    {"action": "replacement", "assignment_item_id": 999999, "qty": 1},
    {"action": "replacement", "assignment_item_id": "x", "qty": 1},
    {"action": "replacement", "assignment_item_id": 1, "qty": 0},
    {"action": "replacement", "assignment_item_id": 1, "qty": -1},
    {"action": "replacement", "assignment_item_id": 1, "qty": "2"},
    {"action": "replacement", "assignment_item_id": 1, "qty": True},
    {"action": "replacement", "assignment_item_id": True, "qty": 1},
], ids=["empty", "no-item", "unknown-item", "item-not-int",
        "qty-zero", "qty-negative", "qty-string", "qty-bool", "item-bool"])
def test_a_malformed_proposal_is_refused_before_any_reservation(
        api, head_db, payload):
    base = world(head_db)
    req = pending(head_db, base, payload=payload)
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "invalid_proposal")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("invalid_proposal")


def test_an_item_owned_by_someone_else_is_refused(api, head_db):
    base = world(head_db)
    with head_db.engine.begin() as c:
        other = add_user(c, "lee")
        other_assignment = add_assignment(c, other)
        others_item = add_item(c, base, qty=2, assignment_id=other_assignment)
    req = pending(head_db, base, item_id=others_item)   # 요청자는 kim, 항목은 lee 것
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "item_not_owned_by_requester")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("item_not_owned_by_requester")


# ── 승인과 예약은 함께 커밋되거나 함께 사라진다 ─────────────────────

def test_a_failed_reservation_rolls_the_approval_back(api, head_db):
    base = world(head_db)
    req = pending(head_db, base)
    with head_db.engine.begin() as c:
        # 예약 삽입을 일부러 실패시킨다. NOT VALID 여도 새 행에는 적용된다.
        c.execute(text(
            "ALTER TABLE stock_reservation "
            "ADD CONSTRAINT boom CHECK (false) NOT VALID"))
    before = snapshot(head_db)

    response = decide(api, req["proposal_id"], base["operator_id"])

    assert response.status_code == 500
    assert snapshot(head_db) == before
    assert audit(head_db) == []
