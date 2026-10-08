"""수동 해제와 승인 철회 (ADR-002, 4단계).

해제는 아직 실행되지 않은 예약을 푼다. 한 트랜잭션에서 예약을 released 로,
그 예약에 연결된 승인을 철회로, 요청을 needs_review 로 바꾼다. 승인 기록은 지우지
않는다. `api` 는 실제 get_conn(성공하면 커밋, 예외면 롤백)으로 도는 클라이언트라서
감사 기록이 정말 남는지까지 확인한다.
"""

import pytest
from sqlalchemy import text

from tests.seed import (
    add_item, add_user, add_version, approve_pending, insert_reservation, revoke,
    rows, scalar, seed_base, seed_pending,
)
from tests.seed import audit_log_rows as audit
from tests.seed import db_snapshot as snapshot

REASON = "고장이 아니라 사용자 설정 문제로 확인됨"


# ── 도우미 ──────────────────────────────────────────────────────────

def approved_case(db, *, stock=5, item_qty=2, qty=1):
    with db.engine.begin() as c:
        base = seed_base(c, stock=stock, item_qty=item_qty)
        approved = approve_pending(c, base, seed_pending(c, base, qty=qty))
    return base, approved


def release(api, proposal_id, user_id, body=None):
    return api.post(
        f"/proposals/{proposal_id}/release",
        headers={"x-user-id": str(user_id)},
        json={"reason": REASON} if body is None else body)


def execute(api, request_id, user_id):
    return api.post("/executions", headers={"x-user-id": str(user_id)},
                    json={"request_id": request_id, "proposal_version": 1})


def assert_refused(response, status, reason_code):
    assert response.status_code == status
    body = response.json()
    assert body["outcome"] == "refused"
    assert body["reason_code"] == reason_code
    assert isinstance(body["detail"], str) and body["detail"]


def new_approval(c, base, pend, decision="approve"):
    return c.execute(text(
        "INSERT INTO approval (request_id, proposal_id, proposal_version, "
        " payload_digest, approver_id, decision) "
        "VALUES (:r, :p, 1, 'd1', :o, CAST(:d AS approval_decision)) RETURNING id"
    ), {"r": pend["request_id"], "p": pend["proposal_id"],
        "o": base["operator_id"], "d": decision}).scalar_one()


T1 = "2026-01-01 00:00:00+00"
T2 = "2026-01-02 00:00:00+00"

# 배치: (released 예약의 created_at, held 예약의 created_at, held 를 먼저 만드는가)
LAYOUTS = {
    # 시각이 완전히 같다. id 로만 순서가 갈린다(id 가 큰 held 가 최신).
    "same-created_at": (T1, T1, False),
    # 시각 순서와 id 순서가 같은 방향이다(held 가 더 늦고 id 도 크다).
    "older-first": (T1, T2, False),
    # 시각 순서와 id 순서가 어긋난다. held 는 id 가 작지만 created_at 이 더 늦다.
    # id 만 보면 released 를 고르고, created_at 을 먼저 봐야 held 를 고른다.
    "created_at-beats-id": (T1, T2, True),
}


def history(db, *, layout):
    """한 처리안의 이력: released 예약(철회된 승인)과 held 예약(유효한 승인).

    거절 기록도 하나 섞는다. created_at 은 테스트가 직접 정한다. 빠르게 연속 실행된
    트랜잭션의 실제 시각 차이나 now() 에 기대면 결과가 우연에 좌우된다.
    """
    released_at, held_at, held_first = LAYOUTS[layout]
    with db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=5)
        pend = seed_pending(c, base, qty=1)
        # 승인은 먼저 모두 만든다. 철회된 것과 유효한 것은 동시에 활성일 수 없다.
        a_released = new_approval(c, base, pend)
        revoke(c, base, a_released, "오래된 사유")
        new_approval(c, base, pend, decision="reject")
        a_held = new_approval(c, base, pend)

        def reservation(approval_id, status):
            return insert_reservation(
                c, base, {**pend, "approval_id": approval_id, "execution_key": None},
                status=status, assignment_item_id=pend["item_id"])

        if held_first:
            r_held = reservation(a_held, "held")
            r_released = reservation(a_released, "released")
        else:
            r_released = reservation(a_released, "released")
            r_held = reservation(a_held, "held")
        for rid, ts in ((r_released, released_at), (r_held, held_at)):
            c.execute(text("UPDATE stock_reservation "
                           "SET created_at = CAST(:t AS timestamptz) WHERE id = :i"),
                      {"t": ts, "i": rid})
        c.execute(text("UPDATE request SET state = 'ready_to_execute' "
                       "WHERE id = :r"), {"r": pend["request_id"]})
    return {"base": base, "pend": pend, "approval_released": a_released,
            "approval_held": a_held, "released": r_released, "held": r_held}


# ── 정상 ────────────────────────────────────────────────────────────

def test_release_frees_the_reservation_and_revokes_the_approval(api, head_db):
    base, case = approved_case(head_db)
    stock_before = snapshot(head_db)["stock"], snapshot(head_db)["items"]

    response = release(api, case["proposal_id"], base["operator_id"])

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "released"
    assert body["reservation_id"] == case["reservation_id"]
    assert body["request_id"] == case["request_id"]

    assert rows(head_db, "SELECT status, execution_key, resolved_at IS NOT NULL "
                         "FROM stock_reservation") == [("released", None, True)]
    # 승인 기록은 지우지 않고 철회 정보만 덧붙인다
    assert rows(head_db, "SELECT revoked_at IS NOT NULL, revoked_by, revoke_reason "
                         "FROM approval") == [(True, base["operator_id"], REASON)]
    assert rows(head_db, "SELECT state FROM request") == [("needs_review",)]
    # 해제는 재고와 배분량을 건드리지 않는다
    after = snapshot(head_db)
    assert (after["stock"], after["items"]) == stock_before

    records = audit(head_db)
    assert len(records) == 1
    assert records[0]["action"] == "release"
    assert records[0]["target_type"] == "proposal"
    assert records[0]["target_id"] == str(case["proposal_id"])
    assert records[0]["result"] == "allowed"
    assert REASON in records[0]["reason"]


def test_a_released_quantity_can_be_reserved_again(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=1, item_qty=5)
        a = approve_pending(c, base, seed_pending(c, base, qty=1))
        b = seed_pending(c, base, qty=1)
    op = base["operator_id"]

    blocked = api.post(f"/proposals/{b['proposal_id']}/approval",
                       headers={"x-user-id": str(op)},
                       json={"proposal_id": b["proposal_id"], "decision": "approve"})
    assert blocked.status_code == 409           # 재고 1개를 A 가 잡고 있다

    assert release(api, a["proposal_id"], op).status_code == 200
    with head_db.engine.begin() as c:
        again = seed_pending(c, base, qty=1)
    freed = api.post(f"/proposals/{again['proposal_id']}/approval",
                     headers={"x-user-id": str(op)},
                     json={"proposal_id": again["proposal_id"], "decision": "approve"})

    assert freed.status_code == 200


def test_a_released_request_cannot_be_executed(api, head_db):
    base, case = approved_case(head_db)
    release(api, case["proposal_id"], base["operator_id"])
    before = snapshot(head_db)

    response = execute(api, case["request_id"], base["operator_id"])

    assert response.status_code == 409
    assert "상태" in response.json()["detail"]
    assert snapshot(head_db) == before
    assert rows(head_db, "SELECT status FROM stock_reservation") == [("released",)]


# ── 권한과 입력 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("who", ["operator-without-right", "employee"])
def test_missing_permission_is_refused_and_audited(api, head_db, who):
    base, case = approved_case(head_db)
    with head_db.engine.begin() as c:
        user = (add_user(c, "op-no", kind="it_operator", can_approve=False)
                if who == "operator-without-right" else base["employee_id"])
    before = snapshot(head_db)

    response = release(api, case["proposal_id"], user)

    assert_refused(response, 403, "no_permission")
    assert snapshot(head_db) == before
    records = audit(head_db)
    assert len(records) == 1 and records[0]["result"] == "denied"
    assert records[0]["reason"].startswith("no_permission")


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}],
                         ids=["missing", "empty", "blank"])
def test_a_reason_is_required_and_not_audited_when_missing(api, head_db, body):
    base, case = approved_case(head_db)
    before = snapshot(head_db)

    response = release(api, case["proposal_id"], base["operator_id"], body)

    assert response.status_code == 400
    assert snapshot(head_db) == before
    assert audit(head_db) == []


def test_an_unknown_proposal_is_a_plain_404(api, head_db):
    base, _ = approved_case(head_db)

    response = release(api, 999999, base["operator_id"])

    assert response.status_code == 404
    # 라우트가 없을 때의 404 는 detail 이 "Not Found" 다. 구현이 처리안을 찾지
    # 못해서 낸 404 인지 detail 로 구분한다 (승인 API 와 같은 문구).
    assert response.json() == {"detail": "처리안을 찾을 수 없다"}
    assert audit(head_db) == []


# ── 거부: 감사 기록만 남고 아무것도 바뀌지 않는다 ───────────────────

def test_a_consumed_reservation_cannot_be_released(api, head_db):
    base, case = approved_case(head_db)
    assert execute(api, case["request_id"], base["operator_id"]).status_code == 200
    before = snapshot(head_db)

    response = release(api, case["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "already_consumed")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("already_consumed")


def test_a_proposal_that_was_never_reserved_cannot_be_released(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c)
        pend = seed_pending(c, base, qty=1)
    before = snapshot(head_db)

    response = release(api, pend["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "no_reservation")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("no_reservation")


@pytest.mark.parametrize("state", ["outcome_unknown", "needs_review",
                                   "awaiting_approval", "rejected"])
def test_only_a_request_ready_to_execute_can_be_released(api, head_db, state):
    """held 예약이 남은 어긋난 상태. outcome_unknown 은 결과를 먼저 확인해야 한다."""
    base, case = approved_case(head_db)
    with head_db.engine.begin() as c:
        c.execute(text("UPDATE request SET state = CAST(:s AS request_state)"),
                  {"s": state})
    before = snapshot(head_db)

    response = release(api, case["proposal_id"], base["operator_id"])

    assert_refused(response, 409, "not_ready_to_execute")
    assert snapshot(head_db) == before
    assert audit(head_db)[0]["reason"].startswith("not_ready_to_execute")


# ── 멱등 ────────────────────────────────────────────────────────────

def test_releasing_twice_changes_nothing_the_second_time(api, head_db):
    base, case = approved_case(head_db)
    op = base["operator_id"]
    assert release(api, case["proposal_id"], op).status_code == 200
    before, audits = snapshot(head_db), audit(head_db)

    response = release(api, case["proposal_id"], op,
                       {"reason": "같은 요청을 한 번 더"})

    assert response.status_code == 200
    assert response.json()["outcome"] == "already_released"
    assert snapshot(head_db) == before
    assert audit(head_db) == audits           # 아무 일도 안 했으니 기록도 늘지 않는다


# ── 어느 예약을 풀고 어느 승인을 철회하는가 ─────────────────────────

@pytest.mark.parametrize("layout", list(LAYOUTS))
def test_the_latest_reservation_of_the_proposal_is_released(api, head_db, layout):
    """created_at DESC, id DESC 로 결정적으로 고른다.

    시각이 같으면 id 가 큰 쪽, 시각이 다르면 늦은 쪽이다. 두 순서가 어긋나는
    배치에서는 created_at 이 id 보다 앞선다. 시각은 테스트가 직접 정했다.
    """
    h = history(head_db, layout=layout)
    # 사전 조건: 배치가 의도한 모양인지 먼저 확인한다
    assert scalar(head_db, "SELECT count(DISTINCT created_at) FROM stock_reservation"
                  ) == (1 if layout == "same-created_at" else 2)
    if layout == "created_at-beats-id":
        assert h["held"] < h["released"]          # held 는 id 가 작다
    else:
        assert h["held"] > h["released"]
    released_before = rows(head_db, "SELECT status, resolved_at, execution_key "
                                    "FROM stock_reservation WHERE id = :i",
                           i=h["released"])

    response = release(api, h["pend"]["proposal_id"], h["base"]["operator_id"])

    assert response.status_code == 200
    assert response.json()["outcome"] == "released"
    assert response.json()["reservation_id"] == h["held"]
    assert rows(head_db, "SELECT status FROM stock_reservation WHERE id = :i",
                i=h["held"]) == [("released",)]
    assert rows(head_db, "SELECT status, resolved_at, execution_key "
                         "FROM stock_reservation WHERE id = :i",
                i=h["released"]) == released_before


def test_only_the_approval_linked_to_the_reservation_is_revoked(api, head_db):
    h = history(head_db, layout="older-first")
    others_before = rows(
        head_db, "SELECT id, decision, revoked_at, revoked_by, revoke_reason "
                 "FROM approval WHERE id <> :held ORDER BY id",
        held=h["approval_held"])

    release(api, h["pend"]["proposal_id"], h["base"]["operator_id"])

    # 연결된 승인만 철회된다
    assert rows(head_db, "SELECT revoke_reason FROM approval WHERE id = :i",
                i=h["approval_held"]) == [(REASON,)]
    # 같은 처리안의 다른 승인(이미 철회된 것)과 거절 기록은 그대로다. 이미 철회된
    # 승인의 사유와 시각을 덮어쓰면 이력이 거짓이 된다.
    assert rows(
        head_db, "SELECT id, decision, revoked_at, revoked_by, revoke_reason "
                 "FROM approval WHERE id <> :held ORDER BY id",
        held=h["approval_held"]) == others_before


def test_the_reservation_of_another_proposal_is_never_touched(api, head_db):
    """같은 요청의 다른 처리안(v2)에 예약이 있어도, 지정한 처리안의 것만 본다."""
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=5)
        v1 = approve_pending(c, base, seed_pending(c, base, qty=1))
        # v1 은 이미 해제·철회된 이력
        c.execute(text("UPDATE stock_reservation SET status = 'released', "
                       "resolved_at = now() WHERE id = :i"), {"i": v1["reservation_id"]})
        revoke(c, base, v1["approval_id"], "오래된 사유")
        v2 = add_version(c, base, v1["request_id"], version=2)
        v2_reservation = insert_reservation(c, base, v2, status="held")
        c.execute(text("UPDATE request SET state = 'ready_to_execute' "
                       "WHERE id = :r"), {"r": v1["request_id"]})
    op = base["operator_id"]
    before_v2 = rows(head_db, "SELECT status FROM stock_reservation WHERE id = :i",
                     i=v2_reservation)

    stale = release(api, v1["proposal_id"], op)

    assert stale.status_code == 200
    assert stale.json()["outcome"] == "already_released"
    assert rows(head_db, "SELECT status FROM stock_reservation WHERE id = :i",
                i=v2_reservation) == before_v2 == [("held",)]
    assert rows(head_db, "SELECT revoked_at IS NULL FROM approval WHERE id = :i",
                i=v2["approval_id"]) == [(True,)]
    assert rows(head_db, "SELECT state FROM request") == [("ready_to_execute",)]

    current = release(api, v2["proposal_id"], op)

    assert current.json()["outcome"] == "released"
    assert current.json()["reservation_id"] == v2_reservation


# ── 원자성 ──────────────────────────────────────────────────────────

def test_a_failure_while_revoking_rolls_everything_back(api, head_db):
    base, case = approved_case(head_db)
    with head_db.engine.begin() as c:
        # 승인 철회 UPDATE 를 일부러 실패시킨다. NOT VALID 여도 바뀌는 행에는 적용된다.
        c.execute(text("ALTER TABLE approval "
                       "ADD CONSTRAINT boom CHECK (revoked_at IS NULL) NOT VALID"))
    before = snapshot(head_db)

    response = release(api, case["proposal_id"], base["operator_id"])

    assert response.status_code == 500
    assert snapshot(head_db) == before
    assert rows(head_db, "SELECT status FROM stock_reservation") == [("held",)]
    assert audit(head_db) == []
