"""승인 API 본문."""

from pydantic import BaseModel


class ApprovalRequest(BaseModel):
    """approver_id 가 없다.

    승인자는 클라이언트가 정하지 않는다. current_user 가 헤더의 신원으로
    DB 를 조회해 정한 값만 쓴다 (SYS-17b).
    """

    proposal_id: int
    decision: str
