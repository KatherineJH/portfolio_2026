"""요청 처리 워크플로. 노드를 잇고 경로를 정한다."""

import hashlib
import json
from functools import partial

from langgraph.graph import END, START, StateGraph
from sqlalchemy import Connection, text

from app.services.graph_state import RequestState
from app.services.nodes import decide_route, load_assignments, retrieve_policies, pick_policy_refs
from app.tracing import traced

ROUTE_STATE = {
    "need_info": "needs_information",
    "need_review": "needs_review",
    "escalate": "escalated",
}


def _close(conn: Connection, state: RequestState, route: str, reason: str) -> dict:
    """처리안 없이 끝나는 경로. 요청 상태만 바꾼다.

    경로가 바뀌면 인용도 다시 고른다. decide 가 propose 로 보고 replacement
    규정을 골라 뒀는데 여기서 need_review 로 뒤집히면, 그 인용은 더 이상
    이 판정의 근거가 아니다.
    """
    conn.execute(text(
        "UPDATE request SET state = :state, updated_at = now() WHERE id = :id"
    ), {"state": ROUTE_STATE[route], "id": state["request_id"]})
    return {
        "route": route,
        "reason": reason,
        "policy_refs": pick_policy_refs(route, state.get("target_item_id"),
                                        state.get("policies", [])),
    }


def close_case(conn: Connection, route: str, state: RequestState) -> dict:
    """need_info / need_review / escalate 종단 노드."""
    return _close(conn, state, route, state["reason"])


def write_proposal(conn: Connection, state: RequestState) -> dict:
    """처리안을 저장하고 승인 대기로 넘긴다."""
    # 처리안 저장 전 검증
    item_id = state["proposal"]["assignment_item_id"]
    qty = state["proposal"]["qty"]
    # LLM 이 고른 대상이 이 임직원의 것인지, 수량이 가능한지 다시 본다.
    item = next(
        (a for a in state["assignments"] if a["id"] == item_id),
        None,
    )
    if item is None:
        return _close(conn, state, "need_info",
                      "처리안이 가리킨 자산이 이 임직원의 지급 목록에 없다")
    if not isinstance(qty, int) or qty < 1 or qty > item["remaining"]:
        return _close(conn, state, "need_info",
                      f"요청 수량 {qty} 가 미처리 수량 {item['remaining']} 과 맞지 않는다")
    if item["inspection"] != "confirmed_faulty":
        return _close(conn, state, "need_review",
                      f"대상 자산의 점검 상태가 {item['inspection']} 이다")
    
    # payload = {
    #     "action": "replacement",
    #     "assignment_item_id": state["proposal"]["assignment_item_id"],
    #     "qty": state["proposal"]["qty"],
    # }
    payload = {
        "action": "replacement",
        "assignment_item_id": item_id,
        "qty": qty,
    }
    # 키 순서를 고정해야 같은 내용이 같은 해시를 낸다.
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()

    # request_id = conn.execute(text(
    #     "INSERT INTO request (employee_id, state) "
    #     "VALUES (:employee_id, 'awaiting_approval') RETURNING id"
    # ), {"employee_id": state["employee_id"]}).scalar_one()

    request_id = state["request_id"]
    conn.execute(text(
        "UPDATE request SET state = 'awaiting_approval', updated_at = now() "
        "WHERE id = :id"
    ), {"id": request_id})

    proposal_id = conn.execute(text(
        "INSERT INTO proposal "
        "(request_id, version, action_type, payload, payload_digest, policy_refs) "
        "VALUES (:request_id, 1, 'replacement', :payload, :digest, :policy_refs) "
        "RETURNING id"
    ), {
        "request_id": request_id,
        "payload": canonical,
        "digest": digest,
        "policy_refs": json.dumps(state["policy_refs"], ensure_ascii=False),
    }).scalar_one()

    return {
        "request_id": request_id,
        "proposal_id": proposal_id,
        "payload_digest": digest,
    }


def pick_branch(state: RequestState) -> str:
    """decide_route 가 정한 경로를 그래프 분기로 바꾼다."""
    return state["route"]


def build_graph(conn: Connection, trace_engine=None):
    graph = StateGraph(RequestState)

    graph.add_node("load", traced(trace_engine, "load", partial(load_assignments, conn)))
    graph.add_node("retrieve", traced(trace_engine, "retrieve", partial(retrieve_policies, conn)))
    graph.add_node("decide", traced(trace_engine, "decide", decide_route))
    # graph.add_node("need_info", ask_question)
    # graph.add_node("need_review", request_inspection)
    # graph.add_node("escalate", escalate_case)
    graph.add_node("need_info", partial(close_case, conn, "need_info"))
    graph.add_node("need_review", partial(close_case, conn, "need_review"))
    graph.add_node("escalate", partial(close_case, conn, "escalate"))
    # graph.add_node("propose", write_proposal) # deprecated
    graph.add_node("propose", traced(trace_engine, "propose", partial(write_proposal, conn)))

    graph.add_edge(START, "load")
    graph.add_edge("load", "retrieve")
    graph.add_edge("retrieve", "decide")

    graph.add_conditional_edges("decide", pick_branch, {
        "need_info": "need_info",
        "need_review": "need_review",
        "escalate": "escalate",
        "propose": "propose",
    })

    for terminal in ("need_info", "need_review", "escalate", "propose"):
        graph.add_edge(terminal, END)

    return graph.compile()