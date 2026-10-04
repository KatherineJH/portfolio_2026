"""AssetFlow HTTP API. 이 파일은 조립만 한다."""

from fastapi import FastAPI

from app.routers import assignments, executions, health, observability, proposals, requests

app = FastAPI(title="AssetFlow")

app.include_router(health.router)
app.include_router(assignments.router)
app.include_router(proposals.router)
app.include_router(executions.router)
app.include_router(requests.router)
app.include_router(observability.router)
