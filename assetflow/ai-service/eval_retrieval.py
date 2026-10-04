"""규정 검색 평가. 정답 규정이 상위 k 안에 들어오는 비율을 잰다."""

from sqlalchemy import create_engine

from app.services.retrieval import search_policies
from app.settings import settings

# (질문, 정답 규정)
CASES = [
    ("노트북 독이 충전이 안 되는데 교체해 주세요", "REPLACE-FAULTY"),
    ("제가 떨어뜨려서 깨졌는데 제가 물어내야 하나요", "COST-LIABILITY"),
    ("동료 노트북도 같이 교체해 주세요", "OWNERSHIP"),
    ("고장났다고 말씀드렸는데 왜 아직 처리가 안 되나요", "INSPECTION-REQUIRED"),
    ("두 개 다 바꿔 주실 수 있나요", "REPLACE-QTY"),
    ("그 모델 재고가 없다고 들었는데요", "REPLACE-STOCK"),
]


def main() -> None:
    engine = create_engine(settings.database_url)
    hit_at_1 = 0
    hit_at_3 = 0

    with engine.connect() as conn:
        for question, expected in CASES:
            results = search_policies(conn, question=question, limit=3)
            keys = [r["policy_key"] for r in results]

            at_1 = keys[:1] == [expected]
            at_3 = expected in keys
            hit_at_1 += at_1
            hit_at_3 += at_3

            mark = "O" if at_3 else "X"
            rank = keys.index(expected) + 1 if at_3 else "-"
            print(f"{mark} rank={rank}  {expected:20} | {question}")

    total = len(CASES)
    print()
    print(f"recall@1 = {hit_at_1}/{total}")
    print(f"recall@3 = {hit_at_3}/{total}")


if __name__ == "__main__":
    main()