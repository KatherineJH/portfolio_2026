"""최종 평가셋의 형식만 검사한다. 사례 내용은 출력하지 않는다."""

import json
import sys
from pathlib import Path

EVAL_FILE = Path(__file__).resolve().parent.parent / "data" / "eval-final" / "routes.jsonl"

ROUTES = {"propose", "need_info", "need_review", "escalate"}
REQUIRED = {"id", "type", "employee", "message", "expect_route",
            "expect_item", "expect_qty", "expect_policy"}
EMPLOYEES = {"kim", "lee", "park"}
ITEMS = {"LAP-01", "LAP-02", "DOCK-01", "MON-01", "PHN-01"}
POLICIES = {"REPLACE-FAULTY", "REPLACE-QTY", "REPLACE-STOCK",
            "INSPECTION-REQUIRED", "OWNERSHIP", "COST-LIABILITY"}


def main() -> None:
    errors: list[str] = []
    ids: list[str] = []
    routes: dict[str, int] = {}

    with open(EVAL_FILE, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except Exception as exc:
                errors.append(f"{n}행: JSON 이 아니다 ({exc})")
                continue

            missing = REQUIRED - case.keys()
            if missing:
                errors.append(f"{n}행: 필드 없음 {sorted(missing)}")
                continue

            cid = case["id"]
            ids.append(cid)

            if case["employee"] not in EMPLOYEES:
                errors.append(f"{cid}: 시드에 없는 임직원")
            if not isinstance(case["expect_route"], list) or not case["expect_route"]:
                errors.append(f"{cid}: expect_route 는 비어 있지 않은 배열이어야 한다")
            elif set(case["expect_route"]) - ROUTES:
                errors.append(f"{cid}: 없는 route 값")
            else:
                for r in case["expect_route"]:
                    routes[r] = routes.get(r, 0) + 1
            if case["expect_item"] is not None and case["expect_item"] not in ITEMS:
                errors.append(f"{cid}: 시드에 없는 자산 코드")
            if case["expect_policy"] is not None and case["expect_policy"] not in POLICIES:
                errors.append(f"{cid}: 없는 규정 키")
            if not str(case["message"]).strip():
                errors.append(f"{cid}: message 가 비었다")

    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        errors.append(f"id 중복: {dup}")

    print(f"사례 {len(ids)}건")
    for route in sorted(routes):
        print(f"  {route:<13}{routes[route]}")

    if errors:
        print()
        for e in errors:
            print("  !", e)
        sys.exit(1)
    print("형식 이상 없음")


if __name__ == "__main__":
    main()