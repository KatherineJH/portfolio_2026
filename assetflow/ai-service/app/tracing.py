"""노드 실행 기록. 업무 트랜잭션과 분리해서 쓴다."""

import sys
import time
from contextlib import contextmanager
from typing import Any, Callable

from sqlalchemy import Connection, text

from app.services.graph_state import RequestState


def traced(engine, node_name: str, func: Callable) -> Callable:
    """노드를 감싸서 실행 시간과 결과를 남긴다.

    트레이스는 자기 트랜잭션에서 커밋한다. 업무 트랜잭션이 롤백돼도
    기록은 남아야 하기 때문이다 (SYS-18).
    """

    def wrapper(state: RequestState) -> dict:
        started = time.monotonic()
        outcome = "ok"
        error_reason = None
        result: dict[str, Any] = {}

        try:
            result = func(state)
        except Exception as exc:
            outcome = "failed"
            error_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            latency_ms = int((time.monotonic() - started) * 1000)
            _record(engine, state=state, node_name=node_name,
                    latency_ms=latency_ms, outcome=outcome,
                    error_reason=error_reason, result=result)

        return result

    return wrapper


def _record(engine, *, state, node_name, latency_ms, outcome, error_reason, result):
    """기록 실패가 업무를 막지 않는다. 다만 조용히 넘어가지도 않는다."""
    if engine is None:
        return
    try:
        with engine.begin() as conn:
            # 기존 DB에는 node_trace.request_id 외래키가 남아 있다. 트레이스는
            # 별도 트랜잭션에서 기록하므로 아직 커밋되지 않은 request 행을 볼 수
            # 없다. DB를 변경하지 않는 호환 모드에서는 느슨한 참조(NULL)로 쓴다.
            # 후속 스키마처럼 외래키가 제거된 DB에서는 request_id를 그대로 남긴다.
            has_request_fk = bool(conn.execute(text(
                "SELECT EXISTS ("
                "  SELECT 1 FROM pg_constraint "
                "  WHERE conrelid = 'node_trace'::regclass "
                "  AND contype = 'f' AND conname = 'node_trace_request_id_fkey'"
                ")"
            )).scalar_one())
            trace_request_id = None if has_request_fk else (
                result.get("request_id") or state.get("request_id")
            )

            conn.execute(text(
                "INSERT INTO node_trace "
                "(run_id, request_id, node_name, started_at, ended_at, "
                " latency_ms, output_summary, model, prompt_tokens, "
                " completion_tokens, cost_usd, outcome, error_reason) "
                "VALUES (:run_id, :request_id, :node_name, "
                "        now() - make_interval(secs => :seconds), now(), "
                "        :latency_ms, :output_summary, :model, :prompt_tokens, "
                "        :completion_tokens, :cost_usd, :outcome, :error_reason)"
            ), {
                "run_id": state["run_id"],
                "request_id": trace_request_id,
                "node_name": node_name,
                "seconds": latency_ms / 1000,
                "latency_ms": latency_ms,
                "output_summary": _summarise(result),
                "model": result.get("model"),
                "prompt_tokens": result.get("prompt_tokens"),
                "completion_tokens": result.get("completion_tokens"),
                "cost_usd": _cost(result),
                "outcome": outcome,
                "error_reason": error_reason,
            })
    except Exception as exc:
        print(f"[trace] {node_name} 기록 실패: {exc}", file=sys.stderr)
        

# 100만 토큰당 USD. 단가는 직접 확인해서 갱신한다.
PRICING = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}
def _cost(result: dict) -> float | None:
    """토큰 수와 단가로 이 호출의 비용을 계산한다."""
    model = result.get("model")
    price = PRICING.get(model)
    if price is None:
        return None

    prompt = result.get("prompt_tokens") or 0
    completion = result.get("completion_tokens") or 0
    return round(
        prompt / 1_000_000 * price["input"]
        + completion / 1_000_000 * price["output"],
        6,
    )


def _summarise(result: dict) -> str:
    """원문은 저장하지 않는다. 키와 개수만 남긴다."""
    import json

    summary = {}
    for key, value in result.items():
        if isinstance(value, list):
            summary[key] = f"list[{len(value)}]"
        elif isinstance(value, (str, int, type(None))):
            summary[key] = value
        else:
            summary[key] = type(value).__name__
    return json.dumps(summary, ensure_ascii=False)
