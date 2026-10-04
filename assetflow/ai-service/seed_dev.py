"""개발용 시드 데이터. 여러 번 실행해도 같은 결과가 나온다."""

from sqlalchemy import create_engine, text

from app.settings import settings

ASSET_MODELS = [
    ("LAP-01", "ThinkPad T14", "LAPTOP", 3),
    ("LAP-02", "MacBook Air 13", "LAPTOP", 2),
    ("DOCK-01", "USB-C Dock", "PERIPHERAL", 5),
    ("MON-01", "24in Monitor", "PERIPHERAL", 4),
    ("PHN-01", "Galaxy A55", "MOBILE", 2),
]

EMPLOYEES = [
    ("kim", "employee", False),
    ("lee", "employee", False),
    ("park", "employee", False),
    ("op-song", "it_operator", True),
    ("op-jung", "it_operator", False),
]

# (임직원, 자산코드, 지급수량, 개당취득가, 점검상태)
ASSIGNMENTS = [
    ("kim", "LAP-01", 1, 1_600_000, "pending"),
    ("kim", "DOCK-01", 2, 120_000, "confirmed_faulty"),
    ("lee", "LAP-02", 1, 1_900_000, "pending"),
    ("lee", "MON-01", 2, 280_000, "confirmed_faulty"),
    ("park", "PHN-01", 1, 600_000, "confirmed_faulty"),
    ("park", "DOCK-01", 1, 120_000, "rejected"),
]

# (규정키, 버전, 적용일, 조건, 예외, 필요정보, 허용행동, 적용상황)
POLICIES = [
    ("REPLACE-FAULTY", 1, "2026-01-01",
     "담당자 점검에서 고장이 확인된 자산은 동일 모델로 교체한다.",
     "사용자 과실이 확인된 경우 비용 청구 검토 대상이다.",
     "점검 상태, 미처리 수량, 교체 재고",
     "교체 처리안 작성",
     "점검에서 고장 확인됨, 새 것으로 바꿔 달라, 같은 모델로 교체, 수리 말고 교환",
     "replacement"),

    ("REPLACE-QTY", 1, "2026-01-01",
     "교체 수량은 지급 수량에서 이미 처리된 수량을 뺀 값을 넘을 수 없다.",
     None,
     "지급 수량, 기처리 수량",
     "수량 초과 시 추가 질문 또는 이관",
     "두 개 다 바꿔 달라, 몇 개까지 되나요, 지난번에 하나 바꿨는데 또, 남은 수량",
     "quantity"),

    ("REPLACE-STOCK", 1, "2026-01-01",
     "교체 재고가 없으면 요청자에게 알리고 대체 모델 선택을 확인받는다.",
     None,
     "가용 재고",
     "재고 없음 안내 및 대체 확인",
     "재고 없다고 하던데, 다른 모델이라도 괜찮다, 언제 들어오나요, 품절",
     "stock"),

    ("INSPECTION-REQUIRED", 1, "2026-01-01",
     "요청자 진술만으로는 고장으로 인정하지 않는다. 담당자 점검이 선행되어야 한다.",
     None,
     "점검 상태",
     "점검 요청으로 이관",
     "고장났다고 말씀드렸는데 처리가 안 된다, 왜 아직인가요, 확인해 주셨나요, "
     "분명히 안 된다고 했는데, 아직 점검 안 받았다",
     "inspection"),

    ("OWNERSHIP", 1, "2026-01-01",
     "본인에게 지급된 자산에 대해서만 요청을 처리한다.",
     None,
     "지급 소유 관계",
     "소유 미확인 시 처리 차단",
     "동료 것도 같이 해 주세요, 팀원 노트북, 제 것이 아닌데, 다른 사람 자산, "
     "대신 신청합니다",
     "ownership"),

    ("COST-LIABILITY", 1, "2026-01-01",
     "파손 귀책과 비용 청구 기준은 아직 확정되지 않았다.",
     None,
     "귀책 판정 기준",
     "담당자 이관",
     "제가 떨어뜨렸는데 물어내야 하나요, 실수로 망가뜨렸다, 변상 책임, "
     "수리비를 내야 하나요, 제 잘못인가요, 자비 부담",
     "liability"),
]


def main() -> None:
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE app_user, asset_model, assignment, request, policy, "
            "         node_trace, audit_log "
            "RESTART IDENTITY CASCADE"
        ))
        for code, name, category, stock in ASSET_MODELS:
            conn.execute(text(
                "INSERT INTO asset_model (code, name, category) "
                "VALUES (:code, :name, :category)"
            ), {"code": code, "name": name, "category": category})
            conn.execute(text(
                "INSERT INTO asset_stock (asset_model_id, available_qty) "
                "VALUES ((SELECT id FROM asset_model WHERE code = :code), :stock)"
            ), {"code": code, "stock": stock})

        for name, kind, can_approve in EMPLOYEES:
            conn.execute(text(
                "INSERT INTO app_user (kind, display_name, can_approve) "
                "VALUES (:kind, :name, :can_approve)"
            ), {"kind": kind, "name": name, "can_approve": can_approve})

        for employee, asset_code, qty, cost, inspection in ASSIGNMENTS:
            conn.execute(text(
                "INSERT INTO assignment (employee_id, status, assigned_at) "
                "VALUES ((SELECT id FROM app_user WHERE display_name = :employee), "
                "        'handed_over', now())"
            ), {"employee": employee})
            item_id = conn.execute(text(
                "INSERT INTO assignment_item "
                "(assignment_id, asset_model_id, qty, unit_acquired_cost, "
                " handover_status, allocated_qty) "
                "VALUES ((SELECT max(id) FROM assignment), "
                "        (SELECT id FROM asset_model WHERE code = :asset_code), "
                "        :qty, :cost, 'delivered', 0) RETURNING id"
            ), {"asset_code": asset_code, "qty": qty, "cost": cost}).scalar_one()

            if inspection == "pending":
                conn.execute(text(
                    "INSERT INTO inspection (assignment_item_id) VALUES (:item_id)"
                ), {"item_id": item_id})
            else:
                conn.execute(text(
                    "INSERT INTO inspection "
                    "(assignment_item_id, status, inspected_by, inspected_at) "
                    "VALUES (:item_id, :status, "
                    "        (SELECT id FROM app_user WHERE display_name='op-song'), now())"
                ), {"item_id": item_id, "status": inspection})

        for (key, version, effective, condition, exception,
             required, action, situation, subject) in POLICIES:
            conn.execute(text(
                "INSERT INTO policy "
                "(policy_key, version, effective_from, condition_text, "
                " exception_text, required_info, allowed_action, situation_text, "
                " subject) "
                "VALUES (:key, :version, :effective, :condition, "
                "        :exception, :required, :action, :situation, "
                "        CAST(:subject AS policy_subject))"
            ), {"key": key, "version": version, "effective": effective,
                "condition": condition, "exception": exception,
                "required": required, "action": action, "situation": situation,
                "subject": subject})

    # print(f"asset models: {len(ASSET_MODELS)}")
        print(f"asset models: {len(ASSET_MODELS)}, employees: {len(EMPLOYEES)}, assignments: {len(ASSIGNMENTS)}, policies: {len(POLICIES)}")


if __name__ == "__main__":
    main()