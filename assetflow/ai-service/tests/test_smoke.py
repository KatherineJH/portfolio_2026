from sqlalchemy import text


def test_migrations_applied_to_test_database(conn):
    count = conn.execute(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name<>'alembic_version'"
        )
    ).scalar_one()
    assert count == 15