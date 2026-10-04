"""대상 어휘 검증. LLM 도 DB 도 필요 없는 순수 함수 테스트다."""

from app.services.nodes import verify_target
from app.services.nodes import pick_policy_refs
from app.services.nodes import route_from_facts

ASSIGNMENTS = [
    {"id": 1, "code": "LAP-01", "inspection": "pending", "remaining": 1},
    {"id": 2, "code": "DOCK-01", "inspection": "confirmed_faulty", "remaining": 2},
]

CANDIDATES = [
    {"policy_key": "REPLACE-FAULTY", "version": 1, "subject": "replacement"},
    {"policy_key": "INSPECTION-REQUIRED", "version": 1, "subject": "inspection"},
    {"policy_key": "REPLACE-QTY", "version": 1, "subject": "quantity"},
]

FAULTY = {"id": 2, "code": "DOCK-01", "inspection": "confirmed_faulty",
          "remaining": 2, "stock": 5}

PENDING = {"id": 1, "code": "LAP-01", "inspection": "pending",
           "remaining": 1, "stock": 3}

def test_named_asset_is_accepted():
    """요청문이 '독'이라고 말했으면 DOCK-01 은 정당하다."""
    assert verify_target("독이 충전이 안 돼요", ASSIGNMENTS, 2) is None


def test_unnamed_asset_is_rejected():
    """'장비'는 자산 종류가 아니다. 고장난 것이 하나뿐이어도 인정하지 않는다."""
    assert verify_target("지급받은 장비 하나가 고장났어요", ASSIGNMENTS, 2) == \
        "요청문이 이 자산을 가리키는 말을 하지 않았다"


def test_wrong_asset_is_rejected():
    """'노트북'이라고 했는데 독을 골랐다."""
    assert verify_target("노트북이 꺼집니다", ASSIGNMENTS, 2) == \
        "요청문이 이 자산을 가리키는 말을 하지 않았다"


def test_null_target_passes_through():
    """모델이 이미 null 을 골랐으면 검증할 것이 없다."""
    assert verify_target("뭔가 고장났어요", ASSIGNMENTS, None) is None


def test_target_outside_assignments_is_rejected():
    """남의 자산 id 를 골랐다."""
    assert verify_target("독이 고장났어요", ASSIGNMENTS, 999) == \
        "고른 대상이 이 임직원의 지급 목록에 없다"


def test_need_review_cites_inspection_policy():
    """검색 1위가 replacement 여도 need_review 는 inspection 을 인용한다."""
    assert pick_policy_refs("need_review", 2, CANDIDATES) == \
        [{"policy_key": "INSPECTION-REQUIRED", "version": 1}]


def test_quantity_overflow_cites_quantity_policy():
    assert pick_policy_refs("need_info", 2, CANDIDATES) == \
        [{"policy_key": "REPLACE-QTY", "version": 1}]


def test_unknown_target_cites_nothing():
    """대상을 특정 못 했으면 인용할 근거도 없다."""
    assert pick_policy_refs("need_info", None, CANDIDATES) == []


def test_missing_subject_yields_empty():
    """맞는 주제가 후보에 없으면 빈 배열이다. 아무거나 고르지 않는다."""
    assert pick_policy_refs("escalate", 2, CANDIDATES) == []


def test_confirmed_faulty_within_limits_is_proposed():
    assert route_from_facts(FAULTY, 1, False)[0] == "propose"


def test_pending_inspection_needs_review():
    """DEV-02 가 틀렸던 자리다. 값 비교라 흔들릴 여지가 없다."""
    assert route_from_facts(PENDING, 1, False)[0] == "need_review"


def test_quantity_over_remaining_needs_info():
    assert route_from_facts(FAULTY, 3, False)[0] == "need_info"


def test_unknown_target_needs_info():
    assert route_from_facts(None, None, False)[0] == "need_info"


def test_liability_topic_escalates():
    assert route_from_facts(FAULTY, 1, True)[0] == "escalate"


def test_stock_shortage_needs_review():
    scarce = {**FAULTY, "stock": 0}
    assert route_from_facts(scarce, 1, False)[0] == "need_review"