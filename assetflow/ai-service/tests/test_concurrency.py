import threading
import time
from sqlalchemy import text

def test_lost_update_without_row_lock(engine):
    """잠금 없이 읽고-계산하고-쓰면 배분이 사라진다."""
    with engine.begin() as setup:
        setup.execute(text(
            "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'emp')"
        ))
        setup.execute(text(
            "INSERT INTO asset_model (code, name, category) "
            "VALUES ('DOCK-99', 'Dock', 'PERIPHERAL')"
        ))
        setup.execute(text(
            "INSERT INTO assignment (employee_id, status, assigned_at) "
            "VALUES ((SELECT id FROM app_user WHERE display_name='emp'), 'handed_over', now())"
        ))
        item_id = setup.execute(text(
            "INSERT INTO assignment_item "
            "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
            " handover_status, allocated_qty) "
            "VALUES ((SELECT max(id) FROM assignment), "
            "        (SELECT max(id) FROM asset_model), "
            "        2, 120000, 'delivered', 0) RETURNING id"
        )).scalar_one()

    read_sql = text("SELECT allocated_qty FROM assignment_item WHERE id = :id")
    write_sql = text("UPDATE assignment_item SET allocated_qty = :v WHERE id = :id")

    try:
        c1, c2 = engine.connect(), engine.connect()
        t1, t2 = c1.begin(), c2.begin()

        a1 = c1.execute(read_sql, {"id": item_id}).scalar_one()
        a2 = c2.execute(read_sql, {"id": item_id}).scalar_one()

        c1.execute(write_sql, {"v": a1 + 1, "id": item_id})
        t1.commit()
        c2.execute(write_sql, {"v": a2 + 1, "id": item_id})
        t2.commit()
        c1.close()
        c2.close()

        with engine.connect() as check:
            final = check.execute(read_sql, {"id": item_id}).scalar_one()

        assert final == 1, "배분을 2건 했는데 allocated_qty 가 1이 아니다"
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(text(
                "TRUNCATE app_user, asset_model, assignment, assignment_item "
                "RESTART IDENTITY CASCADE"
            ))


def test_row_lock_makes_the_second_reader_wait(engine):
    """T1 이 잠근 동안 T2 의 읽기가 막히고, 풀린 뒤 최신 값을 읽는다."""
    with engine.begin() as setup:
        setup.execute(text(
            "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'emp')"
        ))
        setup.execute(text(
            "INSERT INTO asset_model (code, name, category) "
            "VALUES ('DOCK-99', 'Dock', 'PERIPHERAL')"
        ))
        setup.execute(text(
            "INSERT INTO assignment (employee_id, status, assigned_at) "
            "VALUES ((SELECT id FROM app_user WHERE display_name='emp'), 'handed_over', now())"
        ))
        item_id = setup.execute(text(
            "INSERT INTO assignment_item "
            "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
            " handover_status, allocated_qty) "
            "VALUES ((SELECT max(id) FROM assignment), "
            "        (SELECT max(id) FROM asset_model), "
            "        2, 120000, 'delivered', 0) RETURNING id"
        )).scalar_one()

    lock_sql = text("SELECT allocated_qty FROM assignment_item WHERE id = :id FOR UPDATE")
    write_sql = text("UPDATE assignment_item SET allocated_qty = :v WHERE id = :id")
    read_sql = text("SELECT allocated_qty FROM assignment_item WHERE id = :id")

    seen = {}
    t2_reached_select = threading.Event()

    def second_request():
        with engine.connect() as c2:
            t2 = c2.begin()
            t2_reached_select.set()
            seen["t2_read"] = c2.execute(lock_sql, {"id": item_id}).scalar_one()
            c2.execute(write_sql, {"v": seen["t2_read"] + 1, "id": item_id})
            t2.commit()

    try:
        c1 = engine.connect()
        t1 = c1.begin()
        a1 = c1.execute(lock_sql, {"id": item_id}).scalar_one()

        worker = threading.Thread(target=second_request)
        worker.start()
        t2_reached_select.wait(timeout=5)
        time.sleep(0.3)

        c1.execute(write_sql, {"v": a1 + 1, "id": item_id})
        t1.commit()
        c1.close()

        worker.join(timeout=5)
        assert not worker.is_alive()

        with engine.connect() as check:
            final = check.execute(read_sql, {"id": item_id}).scalar_one()

        assert seen["t2_read"] == 1, "T2 가 기다리지 않고 낡은 값을 읽었다"
        assert final == 2
    finally:
        with engine.begin() as cleanup:
            cleanup.execute(text(
                "TRUNCATE app_user, asset_model, assignment, assignment_item "
                "RESTART IDENTITY CASCADE"
            ))