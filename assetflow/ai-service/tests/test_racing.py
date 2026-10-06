"""동시성 도우미 자체의 검증. 도우미가 실패해도 잠금이 남지 않아야 한다."""

import pytest
from sqlalchemy import text

from tests.racing import run_blocked
from tests.seed import seed_base


def idle_in_transaction(db) -> int:
    with db.engine.connect() as c:
        return c.execute(text(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "  AND state = 'idle in transaction'")).scalar_one()


def test_a_failing_second_side_leaves_no_open_transaction_behind(head_db):
    """두 번째가 잠금을 얻은 뒤 예외를 내도, 롤백하고 연결을 닫아서 잠금이 남지 않는다."""
    with head_db.engine.begin() as c:
        base = seed_base(c)
    lock = text("SELECT 1 FROM assignment_item WHERE id = :i FOR UPDATE")

    def first(c):
        c.execute(lock, {"i": base["item_id"]})

    def second(c):
        c.execute(lock, {"i": base["item_id"]})   # 첫 번째가 커밋하면 풀린다
        raise RuntimeError("boom")

    with pytest.raises(AssertionError, match="boom"):
        run_blocked(head_db, first, second)

    assert idle_in_transaction(head_db) == 0
    with head_db.engine.begin() as c:     # 잠금이 남았다면 여기서 막힌다
        c.execute(text("SET LOCAL lock_timeout = '2s'"))
        c.execute(lock, {"i": base["item_id"]})


def test_join_all_fails_when_a_thread_does_not_finish():
    """제한 시간 안에 끝나지 않는 스레드가 있으면 조용히 넘어가지 않고 실패한다."""
    import threading
    from tests.racing import join_all

    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    done = threading.Thread(target=lambda: None)
    stuck.start()
    done.start()
    try:
        with pytest.raises(AssertionError, match="제한 시간 안에 끝나지 않았다"):
            join_all([stuck, done], timeout=0.3)
    finally:
        release.set()
        stuck.join(2)


def test_join_all_accepts_threads_that_finish():
    import threading
    from tests.racing import join_all

    threads = [threading.Thread(target=lambda: None) for _ in range(3)]
    for t in threads:
        t.start()
    join_all(threads, timeout=2)
