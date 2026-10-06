"""테스트 데이터 생성 함수. 마이그레이션 테스트와 승인·예약 테스트가 함께 쓴다.

새 제약이 생기면 이 한 곳만 고친다. 데이터베이스 격리는 dbkit/conftest 가 맡고,
여기에는 연결을 받아 행을 넣는 함수만 둔다. 이전 스키마(`available_qty`)와
새 스키마(`on_hand_qty`)를 모두 다루므로 재고 컬럼 이름을 인자로 받는다.
"""

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


def _one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


# ── 기본 데이터 ─────────────────────────────────────────────────────

def seed_base(conn, stock_col: str = "on_hand_qty", *, stock: int = 5,
              item_qty: int = 2) -> dict:
    """운영자·임직원·독 모델(재고 `stock`개)·지급 항목 1개(수량 `item_qty`)."""
    operator_id = _one(
        conn, "INSERT INTO app_user (kind, display_name, can_approve) "
              "VALUES ('it_operator', 'op', true) RETURNING id")
    employee_id = _one(
        conn, "INSERT INTO app_user (kind, display_name) "
              "VALUES ('employee', 'kim') RETURNING id")
    model_id = _one(
        conn, "INSERT INTO asset_model (code, name, category) "
              "VALUES ('DOCK-01', 'Dock', 'PERIPHERAL') RETURNING id")
    conn.execute(text(
        f"INSERT INTO asset_stock (asset_model_id, {stock_col}) VALUES (:m, :s)"
    ), {"m": model_id, "s": stock})
    assignment_id = _one(
        conn, "INSERT INTO assignment (employee_id, status, assigned_at) "
              "VALUES (:e, 'handed_over', now()) RETURNING id", e=employee_id)
    base = {"operator_id": operator_id, "employee_id": employee_id,
            "model_id": model_id, "assignment_id": assignment_id}
    base["item_id"] = add_item(conn, base, qty=item_qty)
    return base


def add_item(conn, base: dict, *, qty: int = 2, allocated: int = 0,
             assignment_id: int | None = None) -> int:
    """같은 모델의 지급 항목을 하나 더 만든다."""
    return _one(
        conn, "INSERT INTO assignment_item "
              "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
              " handover_status, allocated_qty) "
              "VALUES (:a, :m, :q, 120000, 'delivered', :al) RETURNING id",
        a=assignment_id or base["assignment_id"], m=base["model_id"],
        q=qty, al=allocated)


def add_user(conn, name: str, *, kind: str = "employee",
             can_approve: bool = False) -> int:
    return _one(
        conn, "INSERT INTO app_user (kind, display_name, can_approve) "
              "VALUES (CAST(:k AS user_kind), :n, :c) RETURNING id",
        k=kind, n=name, c=can_approve)


def add_assignment(conn, employee_id: int) -> int:
    return _one(
        conn, "INSERT INTO assignment (employee_id, status, assigned_at) "
              "VALUES (:e, 'handed_over', now()) RETURNING id", e=employee_id)


# ── 요청·처리안·승인 ────────────────────────────────────────────────

def add_version(conn, base: dict, request_id: int, *, version: int = 1,
                decision: str = "approve",
                execution_key: str | None = None) -> dict:
    """한 요청에 처리안 한 버전과 그 결정(승인/거절), 선택적으로 실행 기록."""
    digest = f"d{version}"
    proposal_id = _one(
        conn, "INSERT INTO proposal "
              "(request_id, version, action_type, payload, payload_digest, "
              " policy_refs) "
              "VALUES (:r, :v, 'replacement', '{}', :d, '[]') RETURNING id",
        r=request_id, v=version, d=digest)
    approval_id = _one(
        conn, "INSERT INTO approval "
              "(request_id, proposal_id, proposal_version, payload_digest, "
              " approver_id, decision) "
              "VALUES (:r, :p, :v, :d, :o, CAST(:dec AS approval_decision)) "
              "RETURNING id",
        r=request_id, p=proposal_id, v=version, d=digest,
        o=base["operator_id"], dec=decision)
    if execution_key:
        conn.execute(text(
            "INSERT INTO request_execution "
            "(execution_key, request_id, proposal_version, action_type, "
            " payload_digest, outcome) "
            "VALUES (:k, :r, :v, 'replacement', :d, 'registered')"
        ), {"k": execution_key, "r": request_id, "v": version, "d": digest})
    return {"request_id": request_id, "proposal_id": proposal_id,
            "approval_id": approval_id, "version": version, "digest": digest,
            "execution_key": execution_key}


def seed_request(conn, base: dict, *, state: str = "ready_to_execute",
                 **kw) -> dict:
    """이미 결정(승인/거절)이 끝난 요청. 승인 대기 요청은 seed_pending 을 쓴다."""
    request_id = _one(
        conn, "INSERT INTO request (employee_id, state) "
              "VALUES (:e, CAST(:s AS request_state)) RETURNING id",
        e=base["employee_id"], s=state)
    return add_version(conn, base, request_id, **kw)


def seed_pending(conn, base: dict, *, state: str = "awaiting_approval",
                 qty: int = 1, item_id: int | None = None,
                 employee_id: int | None = None,
                 payload: dict | None = None,
                 action_type: str = "replacement") -> dict:
    """승인 대기 요청과 처리안(v1). 승인 기록은 만들지 않는다.

    처리안의 payload 는 그래프가 쓰는 모양(`action`, `assignment_item_id`,
    `qty`)이다. `payload` 를 주면 그대로 쓴다(잘못된 처리안을 만들 때).
    """
    item_id = item_id or base["item_id"]
    request_id = _one(
        conn, "INSERT INTO request (employee_id, state) "
              "VALUES (:e, CAST(:s AS request_state)) RETURNING id",
        e=employee_id or base["employee_id"], s=state)
    body = payload if payload is not None else {
        "action": "replacement", "assignment_item_id": item_id, "qty": qty}
    proposal_id = _one(
        conn, "INSERT INTO proposal "
              "(request_id, version, action_type, payload, payload_digest, "
              " policy_refs) "
              "VALUES (:r, 1, CAST(:t AS action_type), CAST(:p AS jsonb), "
              "        'd1', '[]') RETURNING id",
        r=request_id, t=action_type, p=json.dumps(body))
    return {"request_id": request_id, "proposal_id": proposal_id,
            "version": 1, "digest": "d1", "item_id": item_id, "qty": qty}


def approve_pending(conn, base: dict, pending: dict, *,
                    qty: int | None = None) -> dict:
    """승인 서비스가 만드는 것과 같은 결과를 직접 만든다.

    승인 기록, held 예약, ready_to_execute. 서비스를 거치지 않으므로 실행 테스트가
    승인 로직에 기대지 않는다.
    """
    approval_id = _one(
        conn, "INSERT INTO approval "
              "(request_id, proposal_id, proposal_version, payload_digest, "
              " approver_id, decision) "
              "VALUES (:r, :p, :v, :d, :o, 'approve') RETURNING id",
        r=pending["request_id"], p=pending["proposal_id"],
        v=pending["version"], d=pending["digest"], o=base["operator_id"])
    conn.execute(text(
        "UPDATE request SET state = 'ready_to_execute', updated_at = now() "
        "WHERE id = :id"), {"id": pending["request_id"]})
    approved = {**pending, "approval_id": approval_id, "execution_key": None}
    reservation_id = insert_reservation(
        conn, base, approved, qty=qty or pending["qty"],
        assignment_item_id=pending["item_id"])
    return {**approved, "reservation_id": reservation_id}


def revoke(conn, base: dict, approval_id: int, reason: str = "test") -> None:
    conn.execute(text(
        "UPDATE approval SET revoked_at = now(), revoked_by = :o, "
        "revoke_reason = :r WHERE id = :id"
    ), {"o": base["operator_id"], "r": reason, "id": approval_id})


# ── 예약 ────────────────────────────────────────────────────────────

RESERVATION_SQL = text(
    "INSERT INTO stock_reservation "
    "(request_id, proposal_id, proposal_version, payload_digest, approval_id, "
    " assignment_item_id, asset_model_id, qty, status, resolved_at, execution_key) "
    "VALUES (:request_id, :proposal_id, :proposal_version, :payload_digest, "
    "        :approval_id, :assignment_item_id, :asset_model_id, :qty, "
    "        CAST(:status AS reservation_status), :resolved_at, :execution_key) "
    "RETURNING id"
)


def reservation_params(base: dict, req: dict, **override) -> dict:
    status = override.get("status", "held")
    params = {
        "request_id": req["request_id"],
        "proposal_id": req["proposal_id"],
        "proposal_version": req["version"],
        "payload_digest": req["digest"],
        "approval_id": req["approval_id"],
        "assignment_item_id": base["item_id"],
        "asset_model_id": base["model_id"],
        "qty": 1,
        "status": "held",
        "resolved_at": None if status == "held" else datetime.now(timezone.utc),
        "execution_key": req["execution_key"] if status == "consumed" else None,
    }
    params.update(override)
    return params


def insert_reservation(conn, base: dict, req: dict, **override) -> int:
    return conn.execute(
        RESERVATION_SQL, reservation_params(base, req, **override)).scalar_one()


# ── 검사 도우미 ─────────────────────────────────────────────────────

def rows(db, sql: str, **params) -> list[tuple]:
    """새 연결로 읽는다. 커밋된 것만 보인다."""
    with db.engine.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), params)]


def scalar(db, sql: str, **params):
    with db.engine.connect() as c:
        return c.execute(text(sql), params).scalar_one()


def rejected(conn, sql, params=None):
    """제약 위반은 세이브포인트 안에서 확인해 같은 연결로 다음 검사를 이어간다."""
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(sql if not isinstance(sql, str) else text(sql),
                         params or {})
