"""워크플로 노드. 각 노드는 state 를 받아 고친 부분만 돌려준다."""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from sqlalchemy import Connection, text

from app.services.graph_state import RequestState
from app.services.retrieval import search_policies
from app.settings import settings

CHAT_MODEL = "gpt-4o-mini"

# 자산 어휘 사전. 요청문이 이 말을 하지 않았다면 대상을 특정한 것이 아니다.
# 값은 asset_model.code 의 접두어다 (DOCK-01 -> DOCK).
ASSET_TERMS = {
    "독": "DOCK", "도크": "DOCK", "dock": "DOCK", "충전독": "DOCK",
    "노트북": "LAP", "랩탑": "LAP", "맥북": "LAP", "씽크패드": "LAP",
    "모니터": "MON", "디스플레이": "MON", "화면": "MON",
    "휴대폰": "PHN", "핸드폰": "PHN", "폰": "PHN", "갤럭시": "PHN",
}


def verify_target(message: str, assignments: list[dict],
                  target_item_id: int | None) -> str | None:
    """LLM 이 고른 대상이 요청문의 어휘로 뒷받침되는지 확인한다.

    뒷받침되면 None, 아니면 거부 사유를 돌려준다. 모델의 추론을 믿지 않고
    요청자가 실제로 말한 것만 인정한다.
    """
    if target_item_id is None:
        return None

    item = next((a for a in assignments if a["id"] == target_item_id), None)
    if item is None:
        return "고른 대상이 이 임직원의 지급 목록에 없다"

    prefix = item["code"].split("-")[0]
    if not any(term in message for term, p in ASSET_TERMS.items() if p == prefix):
        return "요청문이 이 자산을 가리키는 말을 하지 않았다"
    return None

# route 가 정해지면 인용해야 할 규정의 주제도 정해진다. 업무 규칙이지 추론이 아니다.
ROUTE_SUBJECT = {
    "need_review": "inspection",
    "escalate": "liability",
    "propose": "replacement",
    "need_info": "quantity",   # 대상 불명 need_info 는 아래에서 먼저 걸러진다
}


def pick_policy_refs(route: str, target_item_id: int | None,
                     candidates: list[dict]) -> list[dict]:
    """검색 후보 중 이 경로가 인용해야 할 주제의 규정만 남긴다.

    검색은 주제를 모른 채 가까운 것 3 개를 준다. 무엇을 인용할지는
    route 가 정한다. 모델이 고르지 않는다.
    """
    if target_item_id is None:
        return []          # 대상을 특정 못 했으면 인용할 근거도 없다
    subject = ROUTE_SUBJECT.get(route)
    if subject is None:
        return []
    return [{"policy_key": c["policy_key"], "version": c["version"]}
            for c in candidates if c.get("subject") == subject]


def route_from_facts(item: dict | None, qty: int | None,
                     is_liability_topic: bool) -> tuple[str, str]:
    """경로를 코드가 정한다. 규칙 2 를 뺀 나머지는 전부 값 비교다.

    돌려주는 것은 (route, reason) 이다. reason 도 코드가 만든다 —
    판정과 설명이 어긋날 수 없다.
    """
    if item is None:
        return "need_info", "대상 자산을 특정하지 못했다"
    if qty is None or qty < 1 or qty > item["remaining"]:
        return "need_info", (
            f"요청 수량 {qty} 가 미처리 수량 {item['remaining']} 과 맞지 않는다")
    if is_liability_topic:
        return "escalate", "파손 귀책과 비용 부담은 규정이 확정되지 않았다"
    if item["inspection"] != "confirmed_faulty":
        return "need_review", f"점검 상태가 {item['inspection']} 이다"
    if qty > item.get("stock", 0):
        return "need_review", f"교체 재고가 {item.get('stock', 0)} 개뿐이다"
    return "propose", "점검에서 고장이 확인됐고 수량과 재고가 충분하다"


def load_assignments(conn: Connection, state: RequestState) -> dict:
    """이 임직원에게 지급된 항목과 미처리 수량, 점검 상태를 읽는다."""
    rows = conn.execute(text(
        "SELECT ai.id, m.code, m.name, ai.qty, ai.allocated_qty, "
        "       ai.qty - ai.allocated_qty AS remaining, "
        "       COALESCE(i.status::text, 'pending') AS inspection, "
        "       COALESCE(s.available_qty, 0) AS stock "
        "FROM assignment_item ai "
        "JOIN assignment a ON a.id = ai.assignment_id "
        "JOIN asset_model m ON m.id = ai.asset_model_id "
        "LEFT JOIN inspection i ON i.assignment_item_id = ai.id "
        "LEFT JOIN asset_stock s ON s.asset_model_id = m.id "
        "WHERE a.employee_id = :employee_id "
        "ORDER BY m.code"
    ), {"employee_id": state["employee_id"]}).mappings().all()

    return {"assignments": [dict(row) for row in rows]}


def retrieve_policies(conn: Connection, state: RequestState) -> dict:
    """요청문과 가까운 규정 후보를 찾는다. 고르는 것은 다음 노드의 일이다.

    use_policies 가 False 면 검색하지 않는다. 규정 없는 구성과 비교하기
    위한 스위치이며 기본값은 True 다.
    """
    if not state.get("use_policies", True):
        return {"policies": []}
    results = search_policies(conn, question=state["message"], limit=3)
    return {"policies": results}


# 프롬프트와 후처리를 합친 decide 노드의 동작 버전.
# v9 = v8 프롬프트 + 대상 어휘 검증
DECIDE_VERSION = "v11"

# 판단 노드 (첫 LLM 호출)
SYSTEM_PROMPT = """너는 사내 IT 자산 요청을 분류하는 보조자다.

입력은 두 부분이며 쓰임이 다르다.
- [판정_입력] : route 는 오직 이 안의 값으로만 정한다.
- [인용_후보_규정] : 담당자에게 보여줄 근거를 고르는 데만 쓴다.
  **이 규정 문구는 route 판정의 근거가 아니다.**
  규정이 하나도 없어도 route 는 [판정_입력] 만으로 정해진다.

사실에 없는 것을 추측하지 않는다.
금액 계산, 수량 계산, 권한 판정은 하지 않는다. 이미 계산된 값만 인용한다.

[1단계] 대상 자산을 정한다. 요청문의 어휘로만 좁힌다.
- assignments 의 code, name, category 를 보고 맞춘다.
  "독" 은 Dock, "노트북" 은 Laptop, "모니터" 는 Monitor,
  "휴대폰" "폰" "핸드폰" 은 MOBILE 범주를 뜻한다.
  위에 없는 표현이라도 name 이나 category 로 하나가 특정되면 그것을 고른다.
- 이 단계에서 inspection, remaining, stock 값을 보지 않는다.
  그 값들은 2단계에서 복사하고 3단계에서 판정에 쓴다.
- 아래 경우에는 반드시 target_item_id 를 null 로 둔다.
  · 요청문에 자산 종류가 안 나온다.
    "장비" "기기" "물건" "그거" "뭔가 하나" 는 종류가 아니다.
  · 어휘로 좁힌 결과 후보가 둘 이상 남는다.
    그중 하나만 고장이라는 사실은 좁히는 근거가 아니다.
    요청자가 무엇을 말했는지가 기준이지 무엇이 고장났는지가 아니다.
- 추측해서 고르지 않는다. null 인 것은 실패가 아니라 정상적인 결과다.

[2단계] 대상 항목의 값을 그대로 옮겨 적는다.
- target_inspection 에 그 항목의 inspection 값을 복사한다.
- target_remaining 에 그 항목의 remaining 값을 복사한다.
- target_item_id 가 null 이면 둘 다 null 이다.
- 다른 항목의 값을 쓰지 않는다.

[2-1단계] 요청 수량 qty 를 정한다.
- 요청문에 수량이 있으면 그 수를 쓴다. "2개", "둘 다", "전부" 등.
- 수량이 없으면 1 로 본다. 대부분의 요청은 한 개이며,
  더 필요하면 담당자가 처리안을 수정하면 된다.
- target_item_id 가 null 이면 qty 도 null 이다.

[3단계] 요청의 핵심이 파손 귀책·비용 부담·변상 책임에 관한 것인지만 답한다.
- 그런 주제면 is_liability_topic 을 true 로 둔다.
- 단순 고장·교체 요청이면 false 다.
- 경로를 정하지 않는다. 경로는 위 값들로 서버가 계산한다.

요청문의 말투가 불확실해도(예: "같아요", "인 듯") 그것은 판정 근거가 아니다.

JSON 만 출력한다. 설명을 덧붙이지 않는다.
{
  "target_item_id": 숫자 또는 null,
  "target_inspection": "복사한 inspection 값 또는 null",
  "target_remaining": 숫자 또는 null,
  "qty": 숫자 또는 null,
  "is_liability_topic": true 또는 false,
  "missing": ["부족한 정보"]
}"""

def decide_route(state: RequestState) -> dict:
    """LLM 이 경로를 고른다. 계산과 권한 판정은 하지 않는다."""
    facts = {
        "판정_입력": {
            "message": state["message"],
            "assignments": state["assignments"],
        },
        "인용_후보_규정": [
            {"policy_key": p["policy_key"], "version": p["version"],
             "content": p["content"]}
            for p in state["policies"]
        ],
    }
    llm = ChatOpenAI(model=CHAT_MODEL, temperature=0,
                     api_key=settings.openai_api_key)
    response = llm.invoke([
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(facts, ensure_ascii=False)),
    ])

    parsed = json.loads(response.content)
    usage = response.usage_metadata or {}

    target_item_id = parsed.get("target_item_id")
    rejected = verify_target(state["message"], state["assignments"], target_item_id)
    if rejected is not None:
        target_item_id = None

    item = next((a for a in state["assignments"] if a["id"] == target_item_id), None)
    route, reason = route_from_facts(
        item, parsed.get("qty"), bool(parsed.get("is_liability_topic")))

    if rejected is not None:
        reason = rejected

    return {
        "route": route,
        "reason": reason,
        "target_item_id": target_item_id,
        "target_inspection": item["inspection"] if item else None,
        "target_remaining": item["remaining"] if item else None,
        "missing": parsed.get("missing", []) if route == "need_info" else [],
        "policy_refs": pick_policy_refs(route, target_item_id, state["policies"]),
        "model": CHAT_MODEL,
        "prompt_tokens": usage.get("input_tokens"),
        "completion_tokens": usage.get("output_tokens"),
        "proposal": (
            {"assignment_item_id": target_item_id, "qty": parsed.get("qty")}
            if route == "propose" else None
        ),
    }