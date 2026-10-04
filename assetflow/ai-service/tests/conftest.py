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