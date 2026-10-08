"""동시성 테스트 도우미.

한 트랜잭션이 잠금을 쥔 채 커밋하지 않고, 다른 트랜잭션이 실제로 그 잠금에서
막힌 것을 pg_stat_activity 로 확인한 뒤 첫 트랜잭션을 커밋한다. 우연히 겹치기를
기대하지 않으므로 불안정하지 않다. 잠금이 빠진 구현은 "막히지 않았다"로 실패한다.
"""

import threading
import time

from sqlalchemy import text

WAIT_SECONDS = 10


def actor(user_id: int, *, can_approve: bool = True) -> dict:
    return {"id": user_id, "kind": "it_operator", "can_approve": can_approve}


def wait_until_blocked(db, pid: int) -> bool:
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


def short_lock_timeout(conn, seconds: int = 2) -> None:
    """이 트랜잭션이 잠금을 기다리는 시간을 제한한다. 넘으면 LockNotAvailable 에러."""
    conn.execute(text(f"SET LOCAL lock_timeout = '{seconds}s'"))


def join_all(threads, timeout: float | None = None) -> None:
    """모든 스레드가 제한 시간 안에 끝났는지 확인한다.

    join 만 하고 생존을 보지 않으면, 교착이나 장기 대기로 아직 도는 스레드가 있어도
    errors 가 비어 있다는 이유로 다음 검사로 넘어가거나 DB 조회에서 멈춘다.
    """
    deadline = time.monotonic() + (timeout if timeout is not None else WAIT_SECONDS * 2)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    assert all(not t.is_alive() for t in threads), "동시성 작업이 제한 시간 안에 끝나지 않았다"


def run_blocked(db, first, second, *, between=None):
    """first(conn) 를 첫 트랜잭션에서 실행하고 커밋하지 않은 채 둔다.

    second(conn) 는 별도 연결에서 돌고, 첫 트랜잭션의 잠금에서 막힌 것을
    확인한 뒤에 first 를 커밋한다. (first 의 반환값, second 의 반환값)을 돌려준다.

    `between` 이 있으면 second 가 막힌 것을 확인한 뒤, first 를 커밋하기 전에 실행한다.
    잠금 규칙을 지키지 않는 다른 작성자가 끼어드는 경우를 흉내 낼 때 쓴다.
    주의: second 는 first 가 커밋해야 풀리는데 between 은 그 전에 돈다. between 이
    second 가 쥔 잠금을 기다리면 하네스 안에서 영원히 멈춘다(PG 는 이 순환을 못
    본다). between 안의 SQL 에는 반드시 `short_lock_timeout` 을 건다.

    first 가 예외를 내면 롤백하고 그대로 올린다. second 가 예외를 내면 롤백하고
    연결을 닫은 뒤 테스트를 실패시킨다(교착이나 유니크 위반도 여기서 드러난다).
    어느 쪽이든 잠금을 쥔 채 남지 않는다.
    """
    c1 = db.engine.connect()
    tx1 = c1.begin()
    try:
        r1 = first(c1)
    except Exception:
        tx1.rollback()
        c1.close()
        raise

    box: dict = {}

    def run_second():
        c2 = db.engine.connect()
        tx2 = c2.begin()
        try:
            box["pid"] = c2.execute(text("SELECT pg_backend_pid()")).scalar_one()
            box["result"] = second(c2)
            tx2.commit()
        except Exception as exc:     # noqa: BLE001  테스트가 검사한다
            box["error"] = exc
        finally:
            # 실패해도 잠금을 쥔 채 남지 않게 한다. 남으면 뒤의 검사와 다음 테스트가 막힌다.
            if tx2.is_active:
                tx2.rollback()
            c2.close()

    thread = threading.Thread(target=run_second)
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while "pid" not in box and time.monotonic() < deadline:
        time.sleep(0.01)
    blocked = "pid" in box and wait_until_blocked(db, box["pid"])
    try:
        if between is not None and blocked:
            between()
        tx1.commit()
    finally:
        if tx1.is_active:
            tx1.rollback()
        c1.close()
    thread.join(WAIT_SECONDS)

    assert not thread.is_alive(), "두 번째 트랜잭션이 끝나지 않았다"
    assert "error" not in box, box.get("error")
    assert blocked, "두 번째가 첫 번째의 잠금을 기다리지 않았다 (잠금이 없다)"
    return r1, box["result"]


def check_invariants(db) -> None:
    """데이터가 어떻게 만들어졌든 항상 지켜져야 하는 불변조건 (ADR-002)."""
    def count(c, sql):
        return c.execute(text(sql)).scalar_one()

    with db.engine.connect() as c:
        assert count(c, """
            SELECT count(*) FROM asset_stock s
            WHERE COALESCE((SELECT sum(qty) FROM stock_reservation r
                            WHERE r.asset_model_id = s.asset_model_id
                              AND r.status = 'held'), 0) > s.on_hand_qty
        """) == 0, "held 예약 합계가 보유 재고를 넘었다"
        assert count(c, """
            SELECT count(*) FROM assignment_item ai
            WHERE ai.allocated_qty + COALESCE((SELECT sum(qty)
                  FROM stock_reservation r
                  WHERE r.assignment_item_id = ai.id AND r.status = 'held'), 0)
                  > ai.qty
        """) == 0, "배분량과 held 예약의 합이 지급 수량을 넘었다"
        assert count(c, """
            SELECT count(*) FROM stock_reservation r
            WHERE r.status = 'consumed'
              AND (SELECT count(*) FROM simulated_dispatch d
                   WHERE d.execution_key = r.execution_key) <> 1
        """) == 0, "소비된 예약마다 지급 기록이 정확히 하나여야 한다"


def check_clean_run_counts(db) -> None:
    """이 테스트 데이터셋에서만 성립하는 개수 조건. 보편적인 불변조건이 아니다.

    예약 도입 전에 실행된 기록이 섞인 데이터에서는 registered 요청 수가 소비된
    예약 수보다 많을 수 있다. 그래서 check_invariants 와 분리했다. 이 DB 의 모든
    실행이 이 테스트 안에서 일어났을 때만 쓴다.
    """
    def count(c, sql):
        return c.execute(text(sql)).scalar_one()

    with db.engine.connect() as c:
        consumed = count(c, "SELECT count(*) FROM stock_reservation "
                            "WHERE status = 'consumed'")
        executions = count(c, "SELECT count(*) FROM request_execution")
        dispatches = count(c, "SELECT count(*) FROM simulated_dispatch")
        registered = count(c, "SELECT count(*) FROM request "
                              "WHERE state = 'registered'")
        assert consumed == executions == dispatches == registered, (
            f"소비된 예약 {consumed}, 실행 {executions}, 지급 {dispatches}, "
            f"registered 요청 {registered} 이 서로 다르다")
