"""DB 엔진과 요청별 커넥션."""

from collections.abc import Iterator
from sqlalchemy import Connection, create_engine
from app.settings import settings

# create_engine() is the primary entry point for any SQLAlchemy application, 
# To establish a connection factory between Python and a SQL database.
engine = create_engine(settings.database_url, 
                       pool_pre_ping=True,
                       connect_args={"connect_timeout": 10},) # when timeout, 에러임을 알리기 위해 connect_args에 connect_timeout을 설정.


def get_conn() -> Iterator[Connection]:
    """요청 하나가 트랜잭션 하나다.

    핸들러가 정상 종료하면 커밋, 예외가 나면 롤백한다. 커밋 지점을
    눈에 보이게 두는 것이 중요하다.
    """
    conn = engine.connect()
    tx = conn.begin()
    try:
        yield conn
    except Exception:
        tx.rollback()
        raise
    else:
        tx.commit()
    finally:
        conn.close()