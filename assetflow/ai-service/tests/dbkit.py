"""테스트용 데이터베이스 복제 도구.

같은 컨테이너 안에 테스트마다 별도 데이터베이스를 만든다. 템플릿(이전 리비전,
head)은 세션당 한 번만 만들고, 테스트는 그것을 복제한다. fixture 는 conftest.py
에 있고, 여기에는 순수 함수만 둔다.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
OLD_REV = "ea696572e8b4"


def alembic_cfg(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def head_revision() -> str:
    cfg = alembic_cfg("postgresql+psycopg://unused")
    return ScriptDirectory.from_config(cfg).get_current_head()


def database_url(engine, name: str) -> str:
    return engine.url.set(database=name).render_as_string(hide_password=False)


def create_database(admin, template: str | None = None) -> str:
    name = "mig_" + uuid.uuid4().hex[:12]
    sql = f'CREATE DATABASE "{name}"'
    if template:
        sql += f' TEMPLATE "{template}"'
    with admin.connect() as c:
        c.execute(text(sql))
    return name


def drop_database(admin, name: str) -> None:
    with admin.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@dataclass
class Db:
    url: str
    engine: object
