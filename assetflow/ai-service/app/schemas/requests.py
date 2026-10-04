"""요청 접수 API 본문."""

from pydantic import BaseModel


class IntakeRequest(BaseModel):
    message: str
