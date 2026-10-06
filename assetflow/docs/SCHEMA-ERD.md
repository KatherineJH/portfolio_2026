# AssetFlow 현재 ERD와 상태도

[SCHEMA.md](SCHEMA.md)의 구현 구조를 시각화한 문서다. 실제 제약은 Alembic
마이그레이션을 따른다.

## 업무 ERD

```mermaid
erDiagram
    app_user ||--o{ assignment : owns
    app_user ||--o{ request : submits
    app_user ||--o{ approval : decides
    asset_model ||--|| asset_stock : has
    assignment ||--o{ assignment_item : contains
    asset_model ||--o{ assignment_item : identifies
    assignment_item ||--o| inspection : inspected
    request ||--o{ proposal : versions
    proposal ||--o{ approval : reviewed_as
    approval ||--o| stock_reservation : creates
    request ||--o{ stock_reservation : holds
    proposal ||--o{ stock_reservation : binds
    assignment_item ||--o{ stock_reservation : reserves_item
    asset_stock ||--o{ stock_reservation : reserves_stock
    request ||--o{ request_execution : executes
    request_execution ||--o| stock_reservation : consumes
    request_execution ||--o| simulated_dispatch : registers
    policy ||--o{ policy_chunk : embedded_as
```

## 핵심 테이블

```mermaid
erDiagram
    asset_stock {
        bigint asset_model_id PK
        integer on_hand_qty "실제 보유량"
    }
    assignment_item {
        bigint id PK
        bigint assignment_id FK
        bigint asset_model_id FK
        integer qty "지급 수량"
        integer allocated_qty "실행 완료 수량"
    }
    approval {
        bigint id PK
        bigint request_id FK
        bigint proposal_id FK
        integer proposal_version
        text payload_digest
        approval_decision decision
        timestamptz revoked_at
        bigint revoked_by FK
        text revoke_reason
    }
    stock_reservation {
        bigint id PK
        bigint request_id FK
        bigint proposal_id FK
        integer proposal_version
        text payload_digest
        bigint approval_id FK "UNIQUE"
        bigint assignment_item_id FK
        bigint asset_model_id FK
        integer qty
        reservation_status status "held consumed released"
        timestamptz resolved_at
        text execution_key FK "UNIQUE, nullable"
    }
    request_execution {
        text execution_key PK
        bigint request_id FK
        integer proposal_version
        text payload_digest
        execution_outcome outcome
    }
```

## 수량 관계

```mermaid
flowchart LR
    OH["on_hand_qty"] --> RS["재고 예약 가능량"]
    HS["모델별 held 합계"] -->|차감| RS
    Q["assignment_item.qty"] --> RI["지급 예약 가능량"]
    AL["allocated_qty"] -->|차감| RI
    HI["항목별 held 합계"] -->|차감| RI
    RS --> AP{"둘 다 요청 수량 이상?"}
    RI --> AP
    AP -->|예| H["approval + held 예약"]
    AP -->|아니오| NR["needs_review"]
```

## 예약 상태

```mermaid
stateDiagram-v2
    [*] --> held : 승인 commit
    held --> consumed : 실행 commit
    held --> released : 수동 해제 commit
    consumed --> [*]
    released --> [*]
```

`consumed`와 `released`는 최종 상태다. 재승인하면 과거 행을 재사용하지 않고 새 승인과
새 예약을 만든다.

## 요청 상태

```mermaid
stateDiagram-v2
    [*] --> needs_information
    needs_information --> needs_review : 점검·업무 검토 필요
    needs_information --> escalated : 규정 미정
    needs_information --> awaiting_approval : 처리안 생성
    awaiting_approval --> rejected : 승인 거절
    awaiting_approval --> needs_review : 예약 가능량 부족
    awaiting_approval --> ready_to_execute : 승인과 예약 성공
    ready_to_execute --> registered : 예약 소비와 모의 지급
    ready_to_execute --> needs_review : 예약 해제
    needs_review --> awaiting_approval : 재검토 액션
```

## 무결성 경계

- CHECK: 음수 보유량, 배분 초과, 잘못된 예약 상태 조합과 철회 필드 조합을 거부한다.
- 부분 UNIQUE 인덱스: 요청당 활성 승인 하나와 살아 있는 예약 하나를 보장한다.
- 복합 FK: 예약이 같은 요청·처리안·버전·digest의 승인과 실행만 참조하게 한다.
- 행 잠금: 여러 트랜잭션이 같은 항목과 재고의 예약 가능량을 동시에 소비하지 못하게 한다.
- 멱등 실행 키: 같은 의도의 재시도가 두 번째 지급을 만들지 못하게 한다.
