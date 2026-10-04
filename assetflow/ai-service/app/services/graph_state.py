"""워크플로가 들고 다니는 상태."""

from typing import TypedDict


class RequestState(TypedDict, total=False):
    # 입력
    run_id: str
    employee_id: int
    message: str
    use_policies: bool               # 평가에서 규정 없는 구성을 돌릴 때 False

    # 조회 결과
    assignments: list[dict]          # 이 임직원의 지급 항목들
    target_item: dict | None         # 요청이 가리키는 항목

    # 규정 검색 결과
    policies: list[dict]             # 후보 규정 3개

    # 판단 결과
    target_item_id: int | None       # 요청이 가리키는 지급 항목
    target_inspection: str | None
    target_remaining: int | None
    route: str                       # propose / need_info / need_review / escalate
    missing: list[str]               # 부족한 정보
    proposal: dict | None            # 처리안 내용
    request_id: int | None           # 요청 테이블에 새로 생성된 행의 id
    proposal_id: int | None          # proposal 테이블에 새로 생성된 행의 id    
    payload_digest: str | None       # 처리안 payload 의 해시. write_proposal 에서 계산됨
    policy_refs: list[dict]          # 인용한 규정 key·version
    reason: str                      # 그렇게 판단한 이유