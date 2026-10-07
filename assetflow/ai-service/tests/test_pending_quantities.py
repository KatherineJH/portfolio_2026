"""승인 화면 수량은 보유·예약·신규 예약 가능량을 구분한다."""

from sqlalchemy import text

from app.services.execution import execute_replacement
from app.services.release import release_reservation
from tests.racing import actor
from tests.seed import add_item, add_user, approve_pending, seed_base, seed_pending


def _items(api, state, user_id):
    response = api.get("/proposals/pending", params={"state": state},
                       headers={"x-user-id": str(user_id)})
    assert response.status_code == 200
    return response.json()["items"]


def test_pending_quantities_count_only_held_reservations(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=10, item_qty=5)
        item_b = add_item(c, base, qty=5)
        item_done = add_item(c, base, qty=5)
        a = approve_pending(c, base, seed_pending(c, base, qty=2))
        approve_pending(c, base, seed_pending(c, base, qty=1, item_id=item_b))
        done = approve_pending(
            c, base, seed_pending(c, base, qty=1, item_id=item_done))
        execute_replacement(
            c, actor_id=base["operator_id"], request_id=done["request_id"],
            proposal_version=1, assignment_item_id=item_done, qty=1,
            execution_key=f"{done['request_id']}:1:replacement",
            payload_digest=done["digest"])

    rows = _items(api, "ready_to_execute", base["operator_id"])
    row = next(item for item in rows if item["proposal_id"] == a["proposal_id"])
    # 실행된 1개는 보유·기처리에 이미 반영됐고 held 합계에는 다시 들어가지 않는다.
    assert row["on_hand_qty"] == 9
    assert row["held_stock_qty"] == 3
    assert row["reservable_stock_qty"] == 6
    assert row["remaining_item_qty"] == 5
    assert row["held_item_qty"] == 2
    assert row["reservable_item_qty"] == 3
    assert row["reservation_status"] == "held"
    assert row["reservation_qty"] == 2


def test_released_reservations_do_not_reduce_the_quantities(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=4)
        case = approve_pending(c, base, seed_pending(c, base, qty=2))
        release_reservation(
            c, actor=actor(base["operator_id"]),
            proposal_id=case["proposal_id"], reason="테스트 해제")

    row = _items(api, "needs_review", base["operator_id"])[0]
    assert row["held_stock_qty"] == 0
    assert row["reservable_stock_qty"] == 5
    assert row["held_item_qty"] == 0
    assert row["reservable_item_qty"] == 4
    assert row["reservation_status"] == "released"
    assert row["reservation_qty"] == 2


def test_needs_review_lists_a_quantity_refusal_without_a_reservation(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=0, item_qty=2)
        pending = seed_pending(c, base, qty=1)
    response = api.post(
        f"/proposals/{pending['proposal_id']}/approval",
        headers={"x-user-id": str(base["operator_id"])},
        json={"proposal_id": pending["proposal_id"], "decision": "approve"})
    assert response.status_code == 409

    row = _items(api, "needs_review", base["operator_id"])[0]
    assert row["proposal_id"] == pending["proposal_id"]
    assert row["reservation_status"] is None
    assert row["reservation_qty"] is None


def test_unknown_pending_state_is_rejected(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c)
    response = api.get("/proposals/pending", params={"state": "registered"},
                       headers={"x-user-id": str(base["operator_id"])})
    assert response.status_code == 400


def test_pending_list_requires_an_identity(api):
    response = api.get("/proposals/pending")
    assert response.status_code == 422


def test_pending_list_rejects_an_unknown_user(api):
    response = api.get("/proposals/pending", headers={"x-user-id": "999999"})
    assert response.status_code == 401


def test_pending_list_is_hidden_from_employees(api, head_db):
    """요청자 이름이 담긴 목록은 임직원에게 보이지 않는다."""
    with head_db.engine.begin() as c:
        base = seed_base(c)
        seed_pending(c, base, qty=1)
    response = api.get("/proposals/pending",
                       headers={"x-user-id": str(base["employee_id"])})
    assert response.status_code == 403
    assert "items" not in response.json()


def test_an_operator_without_approval_rights_can_still_view(api, head_db):
    """승인 권한은 승인·해제·재검토에서 확인한다. 조회는 IT 담당자면 된다."""
    with head_db.engine.begin() as c:
        base = seed_base(c)
        seed_pending(c, base, qty=1)
        viewer_id = add_user(c, "viewer", kind="it_operator")
    assert len(_items(api, "awaiting_approval", viewer_id)) == 1
