"""재고 예약 스키마 마이그레이션 검증 (ADR-002, 1단계).

DB 복제 장치는 conftest.py(`head_db`, `old_db`), 데이터 생성은 tests/seed.py 에
있다. 이 파일에는 스키마 모양 검사와 제약 테스트만 둔다.
"""

from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError

from tests.dbkit import Db, OLD_REV, alembic_cfg, head_revision
from tests.seed import (
    RESERVATION_SQL, add_version, insert_reservation, rejected, reservation_params,
    revoke, seed_base, seed_request,
)


# ── 스키마 모양 ──────────────────────────────────────────────────────

NEW_CONSTRAINTS = {
    "proposal_id_request_version_key", "assignment_item_id_model_key",
    "request_execution_key_request_version_key", "approval_reservation_key",
}


def inspect_shape(eng) -> dict:
    with eng.connect() as c:
        def scalar(sql, **p):
            return c.execute(text(sql), p).scalar_one()

        return {
            "version": scalar("SELECT version_num FROM alembic_version"),
            "stock_columns": {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'asset_stock'"))},
            "approval_columns": {r[0] for r in c.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'approval'"))},
            "has_reservation_table": scalar(
                "SELECT to_regclass('stock_reservation') IS NOT NULL"),
            "reservation_enum": scalar(
                "SELECT count(*) FROM pg_type WHERE typname = 'reservation_status'"),
            "constraints": {r[0] for r in c.execute(text(
                "SELECT conname FROM pg_constraint"))},
            "indexes": {r[0]: r[1] for r in c.execute(text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public'"))},
        }


def assert_old_shape(eng, version=OLD_REV):
    s = inspect_shape(eng)
    assert s["version"] == version
    assert "available_qty" in s["stock_columns"]
    assert "on_hand_qty" not in s["stock_columns"]
    assert "stock_not_negative" in s["constraints"]
    assert "on_hand_qty_not_negative" not in s["constraints"]
    assert not s["has_reservation_table"]
    assert s["reservation_enum"] == 0
    assert "revoked_at" not in s["approval_columns"]
    assert "revoked_at" not in s["indexes"]["one_active_approval_per_proposal"]
    assert "one_active_approval_per_request" not in s["indexes"]
    assert not (NEW_CONSTRAINTS & s["constraints"])


def assert_new_shape(eng):
    s = inspect_shape(eng)
    assert s["version"] == head_revision()
    assert "on_hand_qty" in s["stock_columns"]
    assert "available_qty" not in s["stock_columns"]
    assert "on_hand_qty_not_negative" in s["constraints"]
    assert "stock_not_negative" not in s["constraints"]
    assert s["has_reservation_table"]
    assert {"revoked_at", "revoked_by", "revoke_reason"} <= s["approval_columns"]
    assert "revoked_at" in s["indexes"]["one_active_approval_per_proposal"]
    assert "revoked_at" in s["indexes"]["one_active_approval_per_request"]
    assert "held" in s["indexes"]["one_live_reservation_per_request"]
    assert NEW_CONSTRAINTS <= s["constraints"]


def upgrade_head(db: Db) -> None:
    command.upgrade(alembic_cfg(db.url), "head")


# ── 리비전 ──────────────────────────────────────────────────────────

def test_new_revision_follows_the_previous_head():
    cfg = alembic_cfg("postgresql+psycopg://unused")
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == [head_revision()]
    assert head_revision() != OLD_REV
    assert script.get_revision(head_revision()).down_revision == OLD_REV


# ── 사전검사: 막아야 하는 경우 ──────────────────────────────────────

@pytest.mark.parametrize("state", ["ready_to_execute", "outcome_unknown"])
def test_precheck_blocks_an_executable_approval_without_execution(old_db, state):
    with old_db.engine.begin() as conn:
        base = seed_base(conn, "available_qty")
        seed_request(conn, base, state=state)

    with pytest.raises(RuntimeError, match="active approval"):
        upgrade_head(old_db)

    assert_old_shape(old_db.engine)


def test_precheck_names_the_blocking_requests(old_db):
    with old_db.engine.begin() as conn:
        base = seed_base(conn, "available_qty")
        req = seed_request(conn, base, state="ready_to_execute")

    with pytest.raises(RuntimeError) as err:
        upgrade_head(old_db)

    assert str(req["request_id"]) in str(err.value)


def test_precheck_blocks_a_request_with_two_approvals(old_db):
    """요청 단위 유일 인덱스를 만들 수 없는 데이터도 미리 막는다."""
    with old_db.engine.begin() as conn:
        base = seed_base(conn, "available_qty")
        first = seed_request(conn, base, state="registered", execution_key="k-1")
        add_version(conn, base, first["request_id"], version=2,
                    execution_key="k-2")

    with pytest.raises(RuntimeError, match="more than one"):
        upgrade_head(old_db)

    assert_old_shape(old_db.engine)


# ── 사전검사: 통과해야 하는 경우 ────────────────────────────────────

def test_empty_database_upgrades(old_db):
    upgrade_head(old_db)
    assert_new_shape(old_db.engine)


def test_executed_request_passes_and_stock_is_kept(old_db):
    with old_db.engine.begin() as conn:
        base = seed_base(conn, "available_qty")
        seed_request(conn, base, state="registered", execution_key="k-1")

    upgrade_head(old_db)

    assert_new_shape(old_db.engine)
    with old_db.engine.connect() as conn:
        assert conn.execute(text(
            "SELECT on_hand_qty FROM asset_stock")).scalar_one() == 5
        assert conn.execute(text("SELECT count(*) FROM stock_reservation")
                            ).scalar_one() == 0


def test_executed_request_still_in_ready_state_passes(old_db):
    """실행 기록이 있으면 상태 표기와 무관하게 앞으로 실행될 승인이 아니다."""
    with old_db.engine.begin() as conn:
        base = seed_base(conn, "available_qty")
        seed_request(conn, base, state="ready_to_execute", execution_key="k-1")

    upgrade_head(old_db)
    assert_new_shape(old_db.engine)


def test_rejection_only_passes(old_db):
    with old_db.engine.begin() as conn:
        base = seed_base(conn, "available_qty")
        seed_request(conn, base, state="rejected", decision="reject")

    upgrade_head(old_db)
    assert_new_shape(old_db.engine)


# ── 예약 테이블 ─────────────────────────────────────────────────────

@pytest.fixture
def world(head_db):
    """head 스키마에 기본 데이터와 승인된 요청 하나. 한 트랜잭션, 끝나면 롤백."""
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base, execution_key="k-1")
        yield conn, base, req
        tx.rollback()


def test_a_valid_held_reservation_is_accepted(world):
    conn, base, req = world
    assert insert_reservation(conn, base, req) > 0


@pytest.mark.parametrize("qty", [0, -1])
def test_reservation_quantity_must_be_positive(world, qty):
    conn, base, req = world
    rejected(conn, RESERVATION_SQL, reservation_params(base, req, qty=qty))


def test_status_must_be_one_of_the_three_values(world):
    """enum 밖의 값은 제약 위반이 아니라 DataError 로 거부된다."""
    conn, base, req = world
    with pytest.raises(DataError):
        with conn.begin_nested():
            conn.execute(RESERVATION_SQL,
                         reservation_params(base, req, status="expired"))


@pytest.mark.parametrize("override", [
    {"status": "held", "resolved_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
    {"status": "held", "execution_key": "k-1"},
    {"status": "released", "resolved_at": None},
    {"status": "released", "execution_key": "k-1"},
    {"status": "consumed", "execution_key": None},
    {"status": "consumed", "resolved_at": None},
], ids=["held+resolved", "held+key", "released-unresolved", "released+key",
        "consumed-no-key", "consumed-unresolved"])
def test_only_three_status_shapes_are_allowed(world, override):
    conn, base, req = world
    rejected(conn, RESERVATION_SQL, reservation_params(base, req, **override))


def test_released_and_consumed_shapes_are_accepted(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        released = seed_request(conn, base)
        consumed = seed_request(conn, base, execution_key="k-9")
        insert_reservation(conn, base, released, status="released")
        insert_reservation(conn, base, consumed, status="consumed")
        tx.rollback()


def test_one_approval_backs_at_most_one_reservation(world):
    conn, base, req = world
    insert_reservation(conn, base, req, status="released")
    rejected(conn, RESERVATION_SQL, reservation_params(base, req))


def test_reservation_needs_an_approving_decision(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        refused = seed_request(conn, base, state="rejected", decision="reject")
        rejected(conn, RESERVATION_SQL, reservation_params(base, refused))
        tx.rollback()


def test_reservation_must_match_its_approval(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        a = seed_request(conn, base)
        b = seed_request(conn, base)

        # 다른 요청의 값을 섞는다
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, request_id=b["request_id"]))
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, proposal_id=b["proposal_id"]))
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, proposal_version=2))
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, payload_digest="other"))
        # 다른 승인을 가리킨다
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, approval_id=b["approval_id"]))
        tx.rollback()


def test_item_and_model_must_belong_together(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        other_model = conn.execute(text(
            "INSERT INTO asset_model (code, name, category) "
            "VALUES ('MON-01', 'Monitor', 'PERIPHERAL') RETURNING id")).scalar_one()
        conn.execute(text(
            "INSERT INTO asset_stock (asset_model_id, on_hand_qty) VALUES (:m, 3)"
        ), {"m": other_model})
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, req, asset_model_id=other_model))
        tx.rollback()


def test_model_without_a_stock_row_cannot_be_reserved(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        bare_model = conn.execute(text(
            "INSERT INTO asset_model (code, name, category) "
            "VALUES ('LAP-01', 'Laptop', 'LAPTOP') RETURNING id")).scalar_one()
        assignment_id = conn.execute(text(
            "SELECT id FROM assignment LIMIT 1")).scalar_one()
        bare_item = conn.execute(text(
            "INSERT INTO assignment_item "
            "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
            " handover_status, allocated_qty) "
            "VALUES (:a, :m, 1, 1, 'delivered', 0) RETURNING id"
        ), {"a": assignment_id, "m": bare_model}).scalar_one()
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, req, assignment_item_id=bare_item, asset_model_id=bare_model))
        tx.rollback()


@pytest.mark.parametrize("first_status, second_is_allowed", [
    ("held", False), ("consumed", False), ("released", True)])
def test_one_live_reservation_per_request_across_versions(
        head_db, first_status, second_is_allowed):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        v1 = seed_request(conn, base, execution_key="k-1")
        revoke(conn, base, v1["approval_id"])
        v2 = add_version(conn, base, v1["request_id"], version=2)

        insert_reservation(conn, base, v1, status=first_status)
        params = reservation_params(base, v2)
        if second_is_allowed:
            conn.execute(RESERVATION_SQL, params)
        else:
            rejected(conn, RESERVATION_SQL, params)
        tx.rollback()


def test_execution_key_must_point_to_a_real_matching_execution(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        a = seed_request(conn, base, execution_key="k-a")
        b = seed_request(conn, base, execution_key="k-b")

        # 없는 키
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, status="consumed", execution_key="no-such-key"))
        # 다른 요청의 실행 기록
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, status="consumed", execution_key="k-b"))
        # 같은 요청이지만 다른 버전의 실행 기록
        rejected(conn, RESERVATION_SQL, reservation_params(
            base, a, status="consumed", proposal_version=2, execution_key="k-a"))
        tx.rollback()


def test_expected_reservation_constraints_exist(head_db):
    with head_db.engine.connect() as conn:
        names = {r[0] for r in conn.execute(text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'stock_reservation'::regclass"))}
    assert {"reservation_state_shape", "reservation_qty_positive",
            "reservation_approval_key", "reservation_execution_key_key",
            "reservation_approval_fk", "reservation_proposal_fk",
            "reservation_item_fk", "reservation_stock_fk",
            "reservation_execution_fk",
            "reservation_requires_approving_decision"} <= names


# ── 승인 ────────────────────────────────────────────────────────────

def test_a_request_has_one_active_approval_across_proposals(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        first = seed_request(conn, base)
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                add_version(conn, base, first["request_id"], version=2)
        tx.rollback()


def test_a_proposal_has_one_active_approval(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        rejected(conn, text(
            "INSERT INTO approval (request_id, proposal_id, proposal_version, "
            " payload_digest, approver_id, decision) "
            "VALUES (:r, :p, :v, :d, :o, 'approve')"
        ), {"r": req["request_id"], "p": req["proposal_id"], "v": req["version"],
            "d": req["digest"], "o": base["operator_id"]})
        tx.rollback()


def test_rejections_may_repeat(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base, state="rejected", decision="reject")
        for _ in range(2):
            conn.execute(text(
                "INSERT INTO approval (request_id, proposal_id, proposal_version, "
                " payload_digest, approver_id, decision) "
                "VALUES (:r, :p, :v, :d, :o, 'reject')"
            ), {"r": req["request_id"], "p": req["proposal_id"],
                "v": req["version"], "d": req["digest"], "o": base["operator_id"]})
        tx.rollback()


@pytest.mark.parametrize("assignments", [
    "revoked_at = now()",
    "revoked_by = :o",
    "revoke_reason = 'why'",
    "revoked_at = now(), revoked_by = :o",
    "revoked_at = now(), revoke_reason = 'why'",
    "revoked_at = now(), revoked_by = :o, revoke_reason = ''",
    "revoked_at = now(), revoked_by = :o, revoke_reason = '   '",
])
def test_revocation_needs_all_three_fields(head_db, assignments):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        rejected(conn, text(f"UPDATE approval SET {assignments} WHERE id = :id"),
                 {"o": base["operator_id"], "id": req["approval_id"]})
        tx.rollback()


def test_only_an_approving_decision_can_be_revoked(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        refused = seed_request(conn, base, state="rejected", decision="reject")
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                revoke(conn, base, refused["approval_id"])
        tx.rollback()


def test_a_revoked_approval_allows_approving_again(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        revoke(conn, base, req["approval_id"])
        conn.execute(text(
            "INSERT INTO approval (request_id, proposal_id, proposal_version, "
            " payload_digest, approver_id, decision) "
            "VALUES (:r, :p, :v, :d, :o, 'approve')"
        ), {"r": req["request_id"], "p": req["proposal_id"], "v": req["version"],
            "d": req["digest"], "o": base["operator_id"]})
        tx.rollback()


# ── 재고 ────────────────────────────────────────────────────────────

def test_on_hand_quantity_cannot_go_negative(head_db):
    with head_db.engine.connect() as conn:
        tx = conn.begin()
        seed_base(conn, "on_hand_qty")
        rejected(conn, "UPDATE asset_stock SET on_hand_qty = -1")
        tx.rollback()


# ── 되돌리기 ────────────────────────────────────────────────────────

def test_downgrade_of_an_empty_state_restores_the_old_shape(head_db):
    command.downgrade(alembic_cfg(head_db.url), OLD_REV)
    assert_old_shape(head_db.engine)


def test_upgrade_after_downgrade_returns_to_the_new_shape(head_db):
    command.downgrade(alembic_cfg(head_db.url), OLD_REV)
    upgrade_head(head_db)
    assert_new_shape(head_db.engine)


def test_downgrade_keeps_existing_stock_and_approvals(head_db):
    with head_db.engine.begin() as conn:
        base = seed_base(conn, "on_hand_qty")
        seed_request(conn, base, execution_key="k-1")

    command.downgrade(alembic_cfg(head_db.url), OLD_REV)

    with head_db.engine.connect() as conn:
        assert conn.execute(text(
            "SELECT available_qty FROM asset_stock")).scalar_one() == 5
        assert conn.execute(text(
            "SELECT count(*) FROM approval")).scalar_one() == 1


def test_downgrade_is_refused_while_a_reservation_exists(head_db):
    with head_db.engine.begin() as conn:
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        insert_reservation(conn, base, req)

    with pytest.raises(RuntimeError, match="reservation"):
        command.downgrade(alembic_cfg(head_db.url), OLD_REV)

    assert_new_shape(head_db.engine)


def test_downgrade_is_refused_while_a_revoked_approval_exists(head_db):
    with head_db.engine.begin() as conn:
        base = seed_base(conn, "on_hand_qty")
        req = seed_request(conn, base)
        revoke(conn, base, req["approval_id"])

    with pytest.raises(RuntimeError, match="revoked"):
        command.downgrade(alembic_cfg(head_db.url), OLD_REV)

    assert_new_shape(head_db.engine)
