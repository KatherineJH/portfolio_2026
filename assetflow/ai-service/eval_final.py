"""최종 평가셋으로 딱 한 번 측정한다.

eval_routes.py 의 실행 로직을 그대로 가져다 쓴다. 복사하면 두 하니스가
조용히 달라지고, 그때부터 개발셋 점수와 최종셋 점수를 비교할 수 없다.

사례 내용(message, expect_*)을 출력하지 않는다. 이 출력을 그대로
붙여넣어도 평가셋이 새지 않아야 한다.
"""

import argparse
import json
import statistics
from pathlib import Path

from app.db import engine
from app.services.nodes import DECIDE_VERSION
from eval_routes import cost_of, require_clean_seed, run_case

EVAL_FILE = Path(__file__).resolve().parent.parent / "data" / "eval-final" / "routes.jsonl"


def load_cases() -> list[dict]:
    with open(EVAL_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    cases = load_cases()
    with engine.begin() as conn:
        require_clean_seed(conn, True)

    config = f"decide-{DECIDE_VERSION}/rag-top3/eval-final-{len(cases)}"
    print(f"config : {config}")
    print(f"repeat : {args.repeat}")
    print()

    runs, all_ids = [], []
    for i in range(args.repeat):
        rows = [run_case(c, True) for c in cases]
        all_ids += [r["run_id"] for r in rows]
        runs.append(rows)

    n = len(cases)
    print(f"{'id':<9}{'route':<7}{'item':<6}{'pol':<5}  got")
    print("-" * 48)
    for idx, case in enumerate(cases):
        first = runs[0][idx]
        got = sorted({runs[i][idx]["got_route"] for i in range(args.repeat)})
        print(f"{case['id']:<9}"
              f"{'O' if first['route_ok'] else 'X':<7}"
              f"{'O' if first['item_ok'] else 'X':<6}"
              f"{'O' if first['policy_ok'] else 'X':<5}  "
              f"{'/'.join(got)}")

    print()
    for key in ("route_ok", "item_ok", "policy_ok"):
        scores = [sum(r[key] for r in rows) / n for rows in runs]
        spread = (f"  (min {min(scores):.2f} / max {max(scores):.2f})"
                  if len(scores) > 1 else "")
        print(f"{key.replace('_ok', ''):<7}{statistics.mean(scores):.2f}{spread}")
    print(f"cost   ${cost_of(all_ids):.6f}")

    print()
    print("── 실패 사례 ──")
    for idx, case in enumerate(cases):
        r = runs[0][idx]
        if not (r["route_ok"] and r["item_ok"] and r["policy_ok"]):
            print(f"{case['id']:<9}route={r['got_route']:<12}"
                  f"item={r['got_item'] or '-':<9}pol={r['got_policies']}")


if __name__ == "__main__":
    main()