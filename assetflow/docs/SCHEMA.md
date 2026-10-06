# AssetFlow 현재 스키마와 트랜잭션 규칙

상태: 구현 및 로컬 검증 완료(2026-10-06). 실제 DDL의 단일 출처는
`ai-service/migrations/versions/`이며, 예약 변경은
`19e6a6ac7866_stock_reservation_schema.py`에 있다. 그림은
[SCHEMA-ERD.md](SCHEMA-ERD.md), 결정 이유는
[ADR-001](adr/001-claim-execution.md)과
[ADR-002](adr/002-stock-reservation.md)를 따른다.

## 핵심 원칙

- PostgreSQL 업무 레코드가 권위다. LangGraph 상태나 화면 응답을 실행 원장으로 쓰지 않는다.
- 승인은 처리안 버전과 digest에 결속되며, 승인 트랜잭션에서 재고와 지급 가능 수량을 함께 예약한다.
- 실행은 새 수량을 경쟁해서 가져가지 않고 승인에 연결된 `held` 예약을 `consumed`로 바꾼다.
- 모든 변경 경로는 필요한 행을 `request → assignment_item → asset_stock → stock_reservation` 순서로 잠근다.
- 성공 HTTP 응답은 DB commit 이후에만 전송한다. DB 연결 실패는 상태를 추측하지 않고 503을 반환한다.
- 모든 데이터와 정책은 합성이며 `simulated_dispatch`는 외부 지급 시스템이 아니다.

## 수량 모델

`asset_stock.on_hand_qty`는 실제 보유량이다. 예약 생성과 해제는 이 값을 바꾸지 않고,
실행이 commit될 때만 감소시킨다.

```text
지급 미처리 수량 = assignment_item.qty - allocated_qty
지급 예약 가능량 = 지급 미처리 수량 - 해당 항목 held 예약 합계
재고 예약 가능량 = asset_stock.on_hand_qty - 해당 모델 held 예약 합계
```

승인은 두 예약 가능량이 모두 요청 수량 이상일 때만 성공한다. 부족하면 승인·예약은
생성하지 않고 요청을 `needs_review`로 전환하며 거부 감사 기록을 남긴다.

## 예약과 승인

`approval`은 승인 이력을 삭제하지 않는다. 철회 시 `revoked_at`, `revoked_by`,
`revoke_reason`을 기록한다. 활성 승인은 `decision='approve' AND revoked_at IS NULL`이며
처리안당 하나, 요청당 하나만 허용된다.

`stock_reservation`의 주요 필드는 다음과 같다.

| 필드 | 의미 |
|---|---|
| `request_id`, `proposal_id`, `proposal_version`, `payload_digest` | 승인된 의도와의 결속 |
| `approval_id` | 이 예약을 만든 승인. 한 승인당 예약 하나 |
| `assignment_item_id`, `asset_model_id`, `qty` | 예약 대상과 수량 |
| `status` | `held`, `consumed`, `released` |
| `resolved_at` | 소비·해제 시각. `held`이면 NULL |
| `execution_key` | 소비된 예약의 실행 원장 키 |

DB 제약은 다음을 보장한다.

- 수량은 양수다.
- 요청당 살아 있는 예약(`held` 또는 `consumed`)은 하나다.
- 예약은 동일 요청·처리안·버전·digest의 승인만 참조한다.
- 예약은 실제 지급 항목과 같은 모델의 재고만 참조한다.
- `held`는 `resolved_at`과 `execution_key`가 없고, `released`는 해제 시각만,
  `consumed`는 해소 시각과 실행 키를 모두 가진다.
- 소비된 예약의 실행 키는 같은 요청과 처리안 버전의 `request_execution`을 참조한다.

## 상태 전이

```text
needs_information
  ├─ needs_review       점검 미완·업무 검토
  ├─ escalated          규정 미정·지원 범위 밖
  └─ awaiting_approval  처리안 생성

awaiting_approval
  ├─ rejected           담당자 거절
  ├─ needs_review       승인 시 수량 부족
  └─ ready_to_execute   승인 + held 예약

ready_to_execute
  ├─ registered         실행 + 예약 consumed
  └─ needs_review       수동 해제 + 승인 철회

needs_review ──(재검토 액션)──> awaiting_approval
```

`outcome_unknown` enum 값은 기존 DB 호환을 위해 남아 있지만 새 코드가 이 상태를 쓰지는
않는다. DB에 접속할 수 없으면 같은 DB에 불명 상태도 기록할 수 없기 때문이다. 연결이
복구되면 `GET /executions/status`로 실행 원장을 조회한다.

## 트랜잭션

### 승인

1. 서버가 승인 권한을 확인한다.
2. `request`를 잠그고 `awaiting_approval`인지 확인한다.
3. `assignment_item`, `asset_stock`을 차례로 잠근다.
4. 두 예약 가능량을 다시 계산한다.
5. 승인, `held` 예약, `ready_to_execute` 전환을 함께 commit한다.

### 실행

1. `request`를 잠그고 동일 실행 키 재시도를 먼저 확인한다.
2. 상태, 소유권, 활성 승인, 처리안 종류·digest·정책 버전을 검증한다.
3. 예약을 사전 확인하고 `assignment_item`, `asset_stock`, 예약을 차례로 잠근다.
4. 예약·수량·보유량을 잠금 아래에서 다시 검증한다.
5. 실행 원장, 예약 소비, 배분 증가, 보유량 감소, 모의 지급, `registered` 전환을
   한 트랜잭션으로 commit한다.

### 해제와 재검토

- 해제는 `held → released`, 연결된 승인 철회, `needs_review`, 감사 기록을 함께 commit한다.
- 자동 만료는 없으며 승인 권한이 있는 담당자가 사유를 입력해 수동 해제한다.
- 재검토는 실행되지 않았고 활성 승인·`held` 예약이 없는 현재 처리안만
  `awaiting_approval`로 되돌린다. 승인이나 예약을 직접 만들지는 않는다.

## 실행 원장과 응답 유실

`execution_key`는 서버가 `request_id:proposal_version:action_type`으로 만든다. 같은 키와
같은 digest 재시도는 기존 결과를 반환하며 두 번째 소비를 만들지 않는다. commit 전
실패는 전체 rollback된다. commit 후 응답만 유실됐다면 원장 조회나 동일 요청 재시도가
기존 결과를 돌려준다.

FastAPI DB 의존성은 function scope를 사용하므로 commit이 성공 응답보다 먼저 끝난다.
연결 수준 장애는 503이며 변경 요청 응답에 일시적인 `outcome: unknown`을 포함하지만,
DB 상태를 `outcome_unknown`으로 쓰지는 않는다.

## 현재 한계

- 자동 예약 만료와 새 처리안 버전 생성 경로는 없다.
- 실행 경로의 거부 감사 기록은 현재 트랜잭션 rollback 범위에 남아 있다.
- 인증은 데모용 `x-user-id` 헤더다.
- 외부 자산·지급 시스템과의 분산 트랜잭션을 주장하지 않는다.
