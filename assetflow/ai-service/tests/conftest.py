from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def engine():
    with PostgresContainer("pgvector/pgvector:pg16", driver="psycopg") as container:
        url = container.get_connection_url()

        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")

        e = create_engine(url)
        yield e
        e.dispose()


@pytest.fixture
def conn(engine):
    connection = engine.connect()
    tx = connection.begin()
    try:
        yield connection
    finally:
        tx.rollback()
        connection.close()


@pytest.fixture
def assignment_item_ids(conn):
    """임직원 1명 · 자산 1종 · 지급 이력 1건을 만들고 id를 돌려준다."""
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name) VALUES ('employee', 'emp')"
    ))
    conn.execute(text(
        "INSERT INTO asset_model (code, name, category) "
        "VALUES ('DOCK-01', 'Dock', 'PERIPHERAL')"
    ))
    conn.execute(text(
        "INSERT INTO assignment (employee_id, status, assigned_at) "
        "VALUES ((SELECT id FROM app_user WHERE display_name='emp'), 'handed_over', now())"
    ))
    return {
        "assignment": conn.execute(text("SELECT max(id) FROM assignment")).scalar_one(),
        "asset_model": conn.execute(text("SELECT max(id) FROM asset_model")).scalar_one(),
    }


@pytest.fixture
def proposal_ids(conn):
    """담당자 2명 · 요청 1건 · 처리안 1건을 만들고 id를 돌려준다."""
    conn.execute(text(
        "INSERT INTO app_user (kind, display_name, can_approve) "
        "VALUES ('it_operator', 'op-a', true), ('it_operator', 'op-b', true)"
    ))
    conn.execute(text(
        "INSERT INTO request (employee_id, state) "
        "VALUES ((SELECT id FROM app_user WHERE display_name='op-a'), 'awaiting_approval')"
    ))
    conn.execute(text(
        "INSERT INTO proposal "
        "(request_id, version, action_type, payload, payload_digest, policy_refs) "
        "VALUES ((SELECT max(id) FROM request), 1, 'replacement', '{}', 'd1', '[]')"
    ))
    return {
        "request": conn.execute(text("SELECT max(id) FROM request")).scalar_one(),
        "proposal": conn.execute(text("SELECT max(id) FROM proposal")).scalar_one(),
        "op_a": conn.execute(text("SELECT id FROM app_user WHERE display_name='op-a'")).scalar_one(),
        "op_b": conn.execute(text("SELECT id FROM app_user WHERE display_name='op-b'")).scalar_one(),
    }

# ── 데이터베이스 복제 (마이그레이션·승인·예약 테스트용) ──────────────
# 공유 `conn` 은 이미 head 인 DB 하나와 롤백 트랜잭션만 준다. 커밋 의미와
# 동시성, 이전 리비전 검증에는 테스트마다 따로 복제한 DB 가 필요하다.

from alembic import command as _alembic_command  # noqa: E402
from sqlalchemy import create_engine as _create_engine  # noqa: E402

from tests import dbkit  # noqa: E402


@pytest.fixture(scope="session")
def admin(engine):
    eng = _create_engine(engine.url, isolation_level="AUTOCOMMIT")
    yield eng
    eng.dispose()


@pytest.fixture(scope="session")
def template_old(engine, admin):
    name = dbkit.create_database(admin)
    _alembic_command.upgrade(
        dbkit.alembic_cfg(dbkit.database_url(engine, name)), dbkit.OLD_REV)
    yield name
    dbkit.drop_database(admin, name)


@pytest.fixture(scope="session")
def template_head(engine, admin):
    name = dbkit.create_database(admin)
    _alembic_command.upgrade(
        dbkit.alembic_cfg(dbkit.database_url(engine, name)), "head")
    yield name
    dbkit.drop_database(admin, name)


def _clone(engine, admin, template):
    name = dbkit.create_database(admin, template)
    url = dbkit.database_url(engine, name)
    return name, dbkit.Db(url=url, engine=_create_engine(url))


@pytest.fixture
def old_db(engine, admin, template_old):
    name, db = _clone(engine, admin, template_old)
    yield db
    db.engine.dispose()
    dbkit.drop_database(admin, name)


@pytest.fixture
def head_db(engine, admin, template_head):
    name, db = _clone(engine, admin, template_head)
    yield db
    db.engine.dispose()
    dbkit.drop_database(admin, name)


@pytest.fixture
def api(head_db, monkeypatch):
    """실제 get_conn(성공하면 커밋, 예외면 롤백)으로 도는 API 클라이언트.

    `app.db.engine` 만 복제 DB 로 바꾼다. get_conn 은 호출 시점에 이 전역을
    읽으므로 코드는 그대로이고, 기존 `client` fixture 와 달리 롤백/커밋이
    테스트 안에서도 운영과 똑같이 일어난다.
    """
    import app.db
    from fastapi.testclient import TestClient
    from app.main import app as fastapi_app

    monkeypatch.setattr(app.db, "engine", head_db.engine)
    yield TestClient(fastapi_app, raise_server_exceptions=False)


@pytest.fixture
def replacement_case(conn):
    """독 2개 지급 · 0개 처리 · 재고 5개 · 승인된 요청 1건(예약 1개 held). AR-01 상황.

    승인 서비스가 만드는 것과 같은 결과를 직접 만든다(승인 기록, held 예약,
    ready_to_execute). 실행 테스트가 승인 로직에 기대지 않게 하려는 것이다.
    """
    from tests.seed import approve_pending, seed_base, seed_pending

    base = seed_base(conn, stock=5, item_qty=2)
    approved = approve_pending(conn, base, seed_pending(conn, base, qty=1))
    return {"base": base, "item_id": base["item_id"],
            "request_id": approved["request_id"],
            "proposal_id": approved["proposal_id"],
            "approval_id": approved["approval_id"],
            "reservation_id": approved["reservation_id"],
            "employee_id": base["employee_id"],
            "operator_id": base["operator_id"]}
