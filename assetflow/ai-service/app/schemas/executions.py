"""실행 API 본문."""

from pydantic import BaseModel


class ExecuteRequest(BaseModel):
    """request_id 와 proposal_version 뿐이다.

    무엇을 몇 개 처리할지는 클라이언트가 정하지 않는다. 승인된 처리안에서
    서버가 읽는다 (단계 84).
    """

    request_id: int
    proposal_version: int
