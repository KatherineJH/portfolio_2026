"""응답 전에 DB 트랜잭션이 끝나는지와 연결 장애 응답을 검증한다."""

from fastapi.routing import APIRoute
from sqlalchemy.exc import OperationalError

from app.db import get_conn
from app.main import app
from tests.seed import db_snapshot, seed_base, seed_pending


def _dependencies(dependant):
    for child in dependant.dependencies:
        yield child
        yield from _dependencies(child)


def _api_routes(router):
    """FastAPI 0.142의 지연 포함 라우터까지 펼친다."""
    for route in router.routes:
        if isinstance(route, APIRoute):
            yield route
        elif hasattr(route, "original_router"):
            yield from _api_routes(route.original_router)


def test_every_database_dependency_has_function_scope():
    """라우터와 current_user의 하위 의존성을 포함해 누락을 막는다."""
    found = []
    for route in _api_routes(app):
        found.extend(d for d in _dependencies(route.dependant)
                     if d.call is get_conn)
    assert found, "get_conn을 쓰는 라우트가 탐지되지 않았다"
    assert all(d.scope == "function" for d in found)


def test_route_and_current_user_share_one_cached_connection(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        pending = seed_pending(c, base, qty=1)
    calls = []

    def counted_connection():
        calls.append("opened")
        with head_db.engine.connect() as conn:
            tx = conn.begin()
            try:
                yield conn
            finally:
                tx.rollback()

    app.dependency_overrides[get_conn] = counted_connection
    try:
        response = api.get(
            "/executions/status",
            params={"request_id": pending["request_id"], "proposal_version": 1},
            headers={"x-user-id": str(base["operator_id"])})
    finally:
        app.dependency_overrides.pop(get_conn, None)

    assert response.status_code == 200
    assert calls == ["opened"]


def test_a_commit_failure_is_returned_before_success(api, head_db):
    with head_db.engine.begin() as c:
        base = seed_base(c, stock=5, item_qty=2)
        pending = seed_pending(c, base, qty=1)
    before = db_snapshot(head_db)

    def fail_commit():
        conn = head_db.engine.connect()
        tx = conn.begin()
        try:
            yield conn
        except Exception:
            tx.rollback()
            raise
        else:
            tx.rollback()
            raise OperationalError("COMMIT", {}, OSError("connection lost"),
                                   connection_invalidated=True)
        finally:
            conn.close()

    app.dependency_overrides[get_conn] = fail_commit
    try:
        response = api.post(
            f"/proposals/{pending['proposal_id']}/approval",
            headers={"x-user-id": str(base["operator_id"])},
            json={"proposal_id": pending["proposal_id"], "decision": "approve"})
    finally:
        app.dependency_overrides.pop(get_conn, None)

    assert response.status_code == 503
    assert response.json()["outcome"] == "unknown"
    assert db_snapshot(head_db) == before


def test_a_non_connection_operational_error_is_not_reported_as_503(api):
    error_type = type("DatabaseError", (Exception,), {"sqlstate": "40P01"})

    def broken():
        raise OperationalError("SELECT", {}, error_type())
        yield  # pragma: no cover

    app.dependency_overrides[get_conn] = broken
    try:
        # 연결 의존성이 먼저 실패하므로 사용자 조회까지 가지 않는다.
        response = api.get("/observability/runs", headers={"x-user-id": "1"})
    finally:
        app.dependency_overrides.pop(get_conn, None)

    assert response.status_code == 500
    assert response.json() == {"detail": "데이터베이스 작업에 실패했다"}


def test_a_read_during_a_connection_failure_is_503_without_an_outcome(api):
    def unavailable():
        raise OperationalError("CONNECT", {}, OSError("connection refused"),
                               connection_invalidated=True)
        yield  # pragma: no cover

    app.dependency_overrides[get_conn] = unavailable
    try:
        # 연결 의존성이 먼저 실패하므로 사용자 조회까지 가지 않는다.
        response = api.get("/observability/runs", headers={"x-user-id": "1"})
    finally:
        app.dependency_overrides.pop(get_conn, None)

    assert response.status_code == 503
    assert response.json()["detail"]
    assert "outcome" not in response.json()
