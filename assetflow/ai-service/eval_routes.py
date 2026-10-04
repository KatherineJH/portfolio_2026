"""고정 평가셋으로 경로 분류를 측정한다.

평가셋 경로를 인자로 받지 않는다. 개발셋과 최종셋을 손으로 지정하다
뒤바꾸는 사고를 막기 위해서다. 최종셋은 단계 107 에서 별도 스크립트로 돈다.
"""

import json
from pathlib import Path
from uuid import uuid4

import argparse
import statistics
from sqlalchemy import text

from app.services.nodes import DECIDE_VERSION
from app.db import engine
from app.services.graph import build_graph

EVAL_FILE = Path(__file__).resolve().parent.parent / "data" / "eval-dev" / "routes.jsonl"


def load_cases() -> list[dict]:
    # encoding 을 명시하지 않으면 Windows 에서 CP949 로 읽어 깨진다.
    with open(EVAL_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def require_clean_seed(conn, use_policies: bool) -> None:
    """평가는 고정된 입력 상태에서만 의미가 있다.

    업무 데이터와 임베딩을 함께 본다. 임베딩이 없으면 검색이 빈 결과를
    돌려주고, 그것은 '규정 없이 돌린 다른 구성'이지 실패가 아니다.
    조용히 다른 실험이 되는 것을 막는다.
    """
    row = conn.execute(text(
        "SELECT (SELECT count(*) FROM assignment_item) AS items, "
        "       (SELECT coalesce(sum(allocated_qty), 0) FROM assignment_item) AS allocated, "
        "       (SELECT count(*) FROM policy) AS policies, "
        "       (SELECT count(*) FROM policy_chunk WHERE embedding IS NOT NULL) AS embedded"
    )).one()

    if row.items != 6 or row.allocated != 0:
        raise SystemExit(
            f"시드 상태가 아니다 (items={row.items}, allocated={row.allocated}). "
            "python seed_dev.py 를 먼저 실행하십시오."
        )
    if use_policies and row.embedded != row.policies:
        raise SystemExit(
            f"규정 임베딩이 없다 (policies={row.policies}, embedded={row.embedded}). "
            "seed_dev.py 가 policy 를 지우면 policy_chunk 도 CASCADE 로 사라진다. "
            "python embed_policies.py 를 실행하십시오."
        )


def asset_code(conn, item_id: int | None) -> str | None:
    if item_id is None:
        return None
    return conn.execute(text(
        "SELECT m.code FROM assignment_item ai "
        "JOIN asset_model m ON m.id = ai.asset_model_id WHERE ai.id = :id"
    ), {"id": item_id}).scalar_one_or_none()


def run_case(case: dict, use_policies: bool) -> dict:
    """사례 하나를 돌리고 롤백한다. 시드가 더럽혀지지 않는다."""
    conn = engine.connect()
    tx = conn.begin()
    run_id = str(uuid4())
    try:
        employee_id = conn.execute(text(
            "SELECT id FROM app_user WHERE display_name = :name"
        ), {"name": case["employee"]}).scalar_one()

        request_id = conn.execute(text(
            "INSERT INTO request (employee_id) VALUES (:e) RETURNING id"
        ), {"e": employee_id}).scalar_one()

        result = build_graph(conn, trace_engine=engine).invoke({
            "run_id": run_id,
            "employee_id": employee_id,
            "message": case["message"],
            "request_id": request_id,
            "use_policies": use_policies,
        })

        got_item = asset_code(conn, result.get("target_item_id"))
        got_policies = [p["policy_key"] for p in result.get("policy_refs", [])]
    finally:
        tx.rollback()
        conn.close()

    return {
        "id": case["id"],
        "type": case["type"],
        "run_id": run_id,
        "route_ok": result["route"] in case["expect_route"],
        "item_ok": got_item == case["expect_item"],
        "policy_ok": (case["expect_policy"] is None
                      or case["expect_policy"] in got_policies),
        "got_route": result["route"],
        "got_item": got_item,
        "got_policies": got_policies,
        "reason": result.get("reason", ""),
    }


def cost_of(run_ids: list[str]) -> float:
    with engine.begin() as conn:
        return float(conn.execute(text(
            "SELECT coalesce(sum(cost_usd), 0) FROM node_trace "
            "WHERE run_id = ANY(:ids)"
        ), {"ids": run_ids}).scalar_one())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-policies", action="store_true",
                        help="규정을 프롬프트에 넣지 않는 구성으로 돌린다")
    parser.add_argument("--repeat", type=int, default=1,
                        help="같은 평가셋을 몇 번 반복할지")
    args = parser.parse_args()
    use_policies = not args.no_policies

    cases = load_cases()
    with engine.begin() as conn:
        require_clean_seed(conn, use_policies)

    config = (f"decide-{DECIDE_VERSION}/"
              f"{'rag-top3' if use_policies else 'no-policies'}"
              f"/eval-dev-{len(cases)}")
    
    print(f"config : {config}")
    print(f"repeat : {args.repeat}")
    print()

    runs, all_ids = [], []
    for i in range(args.repeat):
        rows = [run_case(c, use_policies) for c in cases]
        all_ids += [r["run_id"] for r in rows]
        runs.append(rows)

        print(f"── 실행 {i + 1} ──")
        for r in rows:
            print(f"{r['id']:<8}"
                  f"{'O' if r['route_ok'] else 'X':<7}"
                  f"{'O' if r['item_ok'] else 'X':<6}"
                  f"{'O' if r['policy_ok'] else 'X':<5}  "
                  f"{r['got_route']:<12} {r['type']}")
        print()

    n = len(cases)
    print("── 집계 ──")
    for key in ("route_ok", "item_ok", "policy_ok"):
        scores = [sum(r[key] for r in rows) / n for rows in runs]
        spread = (f"  (min {min(scores):.2f} / max {max(scores):.2f})"
                  if len(scores) > 1 else "")
        print(f"{key.replace('_ok', ''):<7}{statistics.mean(scores):.2f}{spread}")
    print(f"cost   ${cost_of(all_ids):.6f}")

    print()
    print("── 실행마다 결과가 달라진 사례 ──")
    unstable = False
    for idx, case in enumerate(cases):
        got = {runs[i][idx]["got_route"] for i in range(args.repeat)}
        if len(got) > 1:
            unstable = True
            print(f"{case['id']:<8}{sorted(got)}")
    if not unstable:
        print("(없음)")


if __name__ == "__main__":
    main()