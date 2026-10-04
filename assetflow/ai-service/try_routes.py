"""분기 시나리오 확인용. 네 경로가 다 나오는지 본다."""

from sqlalchemy import create_engine
from uuid import uuid4
# from app.nodes import decide_route, load_assignments, retrieve_policies
from app.settings import settings

CASES = [
    (1, "노트북이 고장난 것 같아요. 교체해 주세요."),
    (1, "제가 떨어뜨려서 깨졌는데 제가 물어내야 하나요?"),
    (2, "뭔가 하나 고장났는데 바꿔 주세요"),
    (1, "독 2개 다 교체해 주세요"),
    (2, "모니터가 고장났어요. 하나 교체해 주세요."),
    (3, "휴대폰이 고장났어요. 교체 부탁드립니다."),
]


# def main() -> None:
#     engine = create_engine(settings.database_url)
#     with engine.connect() as conn:
#         for employee_id, message in CASES:
#             state = {"employee_id": employee_id, "message": message}
#             state.update(load_assignments(conn, state))
#             state.update(retrieve_policies(conn, state))
#             result = decide_route(state)
#             target = result.get("target_item_id")
#             print(f"{result['route']:12} | item={str(target):5} | {message[:28]:30} | {result['reason'][:40]}")

def main() -> None:
    from app.graph import build_graph

    engine = create_engine(settings.database_url)
    # with engine.connect() as conn:
    with engine.begin() as conn:    
        # app = build_graph(conn)
        app = build_graph(conn, trace_engine=engine)
        for employee_id, message in CASES:
            print(f"[{message[:20]}...] 처리 중", flush=True)
            # result = app.invoke({"employee_id": employee_id, "message": message})
            result = app.invoke({
                "run_id": str(uuid4()),
                "employee_id": employee_id,
                "message": message,
            })
            target = result.get("target_item_id")
            print(f"{result['route']:12} | item={str(result.get('target_item_id')):5} "
                  f"| insp={str(result.get('target_inspection')):17} "
                  f"| {message[:26]:28}")

if __name__ == "__main__":
    main()