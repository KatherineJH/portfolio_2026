"""전 구간 데모. 요청 접수부터 실행까지 한 번에 돈다.

HTTP API 를 통해 돈다. 그래프만 부르는 eval_routes.py 와 달리 인증·권한·
트랜잭션 경계·상태 전이까지 실제 경로로 지나간다.

사전 조건:
    .venv\\Scripts\\python.exe seed_dev.py
    .venv\\Scripts\\python.exe embed_policies.py
    .venv\\Scripts\\python.exe -m uvicorn app.main:app --reload --port 8000
"""

import json
import sys
from uuid import uuid4

import httpx
from sqlalchemy import text

from app.db import engine

# Windows 콘솔 기본 코드페이지(CP949)로는 한글·특수문자 출력이 깨진다.
# 파일이 아니라 출력 스트림 쪽 문제이므로 여기서 한 번 바꾼다.
sys.stdout.reconfigure(encoding="utf-8")

BASE = "http://localhost:8000"
EMPLOYEE = 1        # kim
OPERATOR_OK = 4     # op-song  (can_approve = true)
OPERATOR_NO = 5     # op-jung  (can_approve = false)
MESSAGE = "독 2개 중 1개가 충전이 안 돼요. 하나만 교체해 주세요."


def step(n: int, title: str) -> None:
    print(f"\n{'─' * 68}\n{n}. {title}\n{'─' * 68}")


def show(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def stock_of(code: str) -> int:
    with engine.begin() as conn:
        return conn.execute(text(
            "SELECT s.on_hand_qty FROM asset_stock s "
            "JOIN asset_model m ON m.id = s.asset_model_id WHERE m.code = :code"
        ), {"code": code}).scalar_one()


def allocated_of(code: str, employee_id: int) -> int:
    with engine.begin() as conn:
        return conn.execute(text(
            "SELECT ai.allocated_qty FROM assignment_item ai "
            "JOIN assignment a ON a.id = ai.assignment_id "
            "JOIN asset_model m ON m.id = ai.asset_model_id "
            "WHERE m.code = :code AND a.employee_id = :employee_id"
        ), {"code": code, "employee_id": employee_id}).scalar_one()


def require_clean_state(client: httpx.Client) -> None:
    try:
        client.get("/health").raise_for_status()
    except Exception:
        sys.exit("서버가 안 떠 있다. uvicorn app.main:app --reload --port 8000")
    with engine.begin() as conn:
        pending = conn.execute(text("SELECT count(*) FROM request")).scalar_one()
        embedded = conn.execute(text(
            "SELECT count(*) FROM policy_chunk WHERE embedding IS NOT NULL"
        )).scalar_one()
    if pending or not embedded:
        sys.exit(
            f"시작 상태가 아니다 (request={pending}, embedded={embedded}). "
            "seed_dev.py 와 embed_policies.py 를 먼저 실행하십시오."
        )


def main() -> None:
    client = httpx.Client(base_url=BASE, timeout=60.0)
    require_clean_state(client)

    before_stock = stock_of("DOCK-01")
    before_alloc = allocated_of("DOCK-01", EMPLOYEE)
    key = str(uuid4())

    step(1, "요청 접수 — LLM 이 대상·수량을 읽고 코드가 경로를 정한다")
    print(f"요청문: {MESSAGE}\n")
    first = client.post(
        "/requests",
        headers={"x-user-id": str(EMPLOYEE), "Idempotency-Key": key},
        json={"message": MESSAGE},
    ).json()
    show(first)

    step(2, "같은 멱등성 키로 재시도 — 그래프를 다시 돌리지 않는다")
    again = client.post(
        "/requests",
        headers={"x-user-id": str(EMPLOYEE), "Idempotency-Key": key},
        json={"message": MESSAGE},
    ).json()
    same = again["run_id"] == first["run_id"] and again["request_id"] == first["request_id"]
    print(f"run_id 동일: {same}   (LLM 을 두 번 부르지 않았다)")

    step(3, "승인 대기 목록 — 담당자가 판단할 사실이 한 화면에")
    pending = client.get("/proposals/pending").json()["items"]
    for item in pending:
        print(f"{item['requester']} / {item['asset_name']} ({item['asset_code']})")
        print(f"  처리안    {item['payload']['qty']}개 교체")
        print(f"  미처리    {item['remaining']}개 · 교체 재고 {item['stock']}개")
        print(f"  근거 규정 {[p['policy_key'] for p in item['policy_refs']]}")
    proposal_id = pending[0]["proposal_id"]
    request_id = pending[0]["request_id"]

    step(4, "권한 없는 담당자가 승인 시도 — 서버가 막는다 (SYS-17b)")
    denied = client.post(
        f"/proposals/{proposal_id}/approval",
        headers={"x-user-id": str(OPERATOR_NO)},
        json={"proposal_id": proposal_id, "decision": "approve"},
    )
    print(f"HTTP {denied.status_code}  {denied.json()}")

    step(5, "권한 있는 담당자가 승인")
    approved = client.post(
        f"/proposals/{proposal_id}/approval",
        headers={"x-user-id": str(OPERATOR_OK)},
        json={"proposal_id": proposal_id, "decision": "approve"},
    ).json()
    show(approved)

    step(6, "실행 — 클라이언트는 무엇을 몇 개 할지 말하지 않는다")
    executed = client.post(
        "/executions",
        headers={"x-user-id": str(OPERATOR_OK)},
        json={"request_id": request_id, "proposal_version": 1},
    ).json()
    show(executed)

    step(7, "같은 실행을 재시도 — 배분은 한 번뿐이다 (SYS-03)")
    retried = client.post(
        "/executions",
        headers={"x-user-id": str(OPERATOR_OK)},
        json={"request_id": request_id, "proposal_version": 1},
    ).json()
    print(f"실행 키 동일: {retried['execution_key'] == executed['execution_key']}")

    step(8, "DB 변화 — 두 번 눌렀지만 한 번만 반영됐다")
    after_stock = stock_of("DOCK-01")
    after_alloc = allocated_of("DOCK-01", EMPLOYEE)
    print(f"  처리 수량   {before_alloc} → {after_alloc}")
    print(f"  교체 재고   {before_stock} → {after_stock}")
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT count(*) FROM request_execution"
        )).scalar_one()
        state = conn.execute(text(
            "SELECT state::text FROM request WHERE id = :id"
        ), {"id": request_id}).scalar_one()
    print(f"  실행 기록   {rows}건")
    print(f"  요청 상태   {state}")

    step(9, "처리 과정 — 노드별 지연·모델·비용")
    trace = client.get(f"/requests/{request_id}/trace").json()["items"]
    print(f"  {'노드':<10}{'결과':<7}{'지연':>8}  {'모델':<14}{'비용':>10}")
    for r in trace:
        cost = f"${r['cost_usd']:.6f}" if r["cost_usd"] else "-"
        print(f"  {r['node_name']:<10}{r['outcome']:<7}{r['latency_ms']:>6}ms  "
              f"{r['model'] or '-':<14}{cost:>10}")

    total = sum(r["cost_usd"] or 0 for r in trace)
    print(f"\n  이 요청 한 건의 LLM 비용: ${total:.6f}")
    client.close()


if __name__ == "__main__":
    main()
