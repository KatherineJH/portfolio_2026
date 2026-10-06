"""AssetFlow HTTP API. 이 파일은 조립과 공통 장애 응답만 담당한다."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

from app.routers import assignments, executions, health, observability, proposals, requests

app = FastAPI(title="AssetFlow")


def _is_connection_failure(exc: OperationalError | InterfaceError) -> bool:
    """SQL/제약 오류와 실제 연결 장애를 구분한다."""
    if isinstance(exc, InterfaceError) or exc.connection_invalidated:
        return True
    sqlstate = getattr(exc.orig, "sqlstate", None)
    # 접속 거부·소켓 단절은 SQLSTATE가 없고, 08 계열은 connection exception이다.
    # 57P01~03은 서버 종료·복구·접속 불가 상태다.
    return (sqlstate is None or str(sqlstate).startswith("08")
            or sqlstate in {"57P01", "57P02", "57P03"})


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def database_error(request: Request, exc: OperationalError | InterfaceError):
    if not _is_connection_failure(exc):
        return JSONResponse(status_code=500,
                            content={"detail": "데이터베이스 작업에 실패했다"})

    content: dict[str, str] = {
        "detail": "데이터베이스에 연결할 수 없다. 잠시 후 다시 시도해야 한다"
    }
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        # 커밋 중 연결이 끊기면 서버도 성공 여부를 단정할 수 없다. 이 값은 DB 상태가
        # 아니라 응답의 일시적 판정이며, 복구 후 상태 조회로 확정한다.
        content["outcome"] = "unknown"
    return JSONResponse(status_code=503, content=content)

app.include_router(health.router)
app.include_router(assignments.router)
app.include_router(proposals.router)
app.include_router(executions.router)
app.include_router(requests.router)
app.include_router(observability.router)
