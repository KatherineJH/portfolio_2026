# AssetFlow 전체 흐름

코드에 실제로 있는 경로만 그린다. 설계 의도나 계획은 넣지 않는다.

근거: `app/routers/`, `app/services/graph.py`, `app/services/nodes.py`, `app/services/execution.py`

---

## 1. 전체 경로 — 요청문 한 줄에서 지급 처리까지

```mermaid
flowchart TD
    U(["임직원이 요청문을 보낸다"]) --> API["POST /requests"]

    API --> IDEM{"Idempotency-Key 가<br/>이미 처리됐나"}
    IDEM -->|있다| REPLAY["저장된 응답을 그대로 반환<br/>LLM 을 부르지 않는다"]
    IDEM -->|없다| INS["request 행 생성<br/>state = needs_information"]
    INS -->|같은 키 동시 도착| C409["409 같은 멱등성 키의<br/>요청이 처리 중이다"]

    INS --> G["LangGraph 실행"]

    subgraph G2 ["워크플로 (노드 4개)"]
        direction TB
        L["load<br/>지급 이력·점검·재고 조회"] --> R["retrieve<br/>pgvector 규정 top-3"]
        R --> D["decide<br/>LLM 호출 1회"]
        D --> V["verify_target<br/>요청문 어휘로 대상 검증"]
        V --> RF["route_from_facts<br/>경로를 코드가 정한다"]
    end

    G --> G2
    RF --> BR{"route"}

    BR -->|need_info| NI["needs_information"]
    BR -->|need_review| NR["needs_review"]
    BR -->|escalate| ES["escalated"]
    BR -->|propose| WP["write_proposal<br/>저장 전 재검증"]

    WP -->|검증 실패| BR2{"재판정"}
    BR2 -->|need_info| NI
    BR2 -->|need_review| NR
    WP -->|통과| PR["proposal 저장 + digest<br/>state = awaiting_approval"]

    PR --> OP(["IT 담당자 화면"])
    OP --> APV["POST /proposals/{id}/approval"]

    APV --> PERM{"can_approve"}
    PERM -->|false| DENY["403 승인 권한이 없다<br/>audit_log 에 거부 기록"]
    DENY --> OP
    PERM -->|true| DEC{"decision"}

    DEC -->|reject| RJ["rejected"]
    DEC -->|approve| RTE["ready_to_execute<br/>approval 에 digest 결속"]

    RTE --> EXE["POST /executions"]
    EXE --> EX["execute_replacement<br/>9단계 검증"]
    EX -->|거부| C409b["409 + audit_log"]
    C409b --> OP
    EX -->|통과| REG["재고 차감 · 배분 증가<br/>simulated_dispatch 기록<br/>state = registered"]

    EX -.->|응답 유실| UNK["outcome_unknown<br/>사람이 확인"]
    UNK -.->|resolve_outcome| REG

    NI --> OP
    NR --> OP
    ES --> OP

    classDef terminal fill:#eef4ff,stroke:#5b7cc2
    classDef danger fill:#fdeeee,stroke:#c25b5b
    class REG,RJ terminal
    class DENY,C409,C409b,UNK danger
```

---

## 2. 요청 상태 기계

`request.state` ENUM 8개가 전부다. 다른 값으로는 갈 수 없다.

```mermaid
stateDiagram-v2
    [*] --> needs_information : POST /requests 가 행을 만든다

    needs_information --> needs_review : decide 가 need_review
    needs_information --> escalated : decide 가 escalate
    needs_information --> awaiting_approval : write_proposal 통과

    awaiting_approval --> rejected : 담당자 거절
    awaiting_approval --> ready_to_execute : 담당자 승인
    ready_to_execute --> registered : 실행 성공
    ready_to_execute --> outcome_unknown : 결과 확인 불가
    outcome_unknown --> registered : resolve_outcome 이 기록 확인

    registered --> [*]
    rejected --> [*]
    needs_review --> [*]
    escalated --> [*]

    note right of needs_information
        기본값. 판단 전 상태이며
        need_info 로 끝나도 여기 남는다
    end note

    note right of outcome_unknown
        실행은 됐을 수도 있다.
        멱등성 키로 다시 물어본다
    end note
```

**`needs_information` 이 시작값이자 종착값**이다. 요청은 사람이 말을 건 순간 만들어지고, 판단 결과가 그 행의 속성으로 붙는다(단계 102).

---

## 3. 시나리오별 타임라인

### AR-01 정상 교체 — `propose` → `registered`

```mermaid
sequenceDiagram
    autonumber
    participant E as 임직원
    participant A as FastAPI
    participant G as LangGraph
    participant L as OpenAI
    participant D as PostgreSQL
    participant O as IT 담당자

    E->>A: POST /requests "독 1개 교체"
    A->>D: INSERT request (needs_information)
    A->>G: invoke(request_id)
    G->>D: load 지급·점검·재고
    G->>L: embed(요청문)
    G->>D: 규정 top-3 (pgvector)
    G->>L: decide 1회 호출
    L-->>G: target_item_id, qty, is_liability
    Note over G: verify_target → route_from_facts<br/>= propose
    G->>D: proposal 저장 + payload_digest
    G->>D: request.state = awaiting_approval
    A-->>E: route=propose, proposal_id
    Note over G,D: node_trace 4행은 별도 트랜잭션으로 커밋

    O->>A: GET /proposals/pending
    A-->>O: 요청자·자산·미처리·재고·근거 규정
    O->>A: POST approval (approve)
    A->>D: approval 저장 (digest 결속)
    A->>D: state = ready_to_execute

    O->>A: POST /executions
    A->>D: FOR UPDATE 잠금 → 재검증
    A->>D: allocated_qty +1, available_qty -1
    A->>D: request_execution, simulated_dispatch
    A->>D: state = registered
    A-->>O: execution_key, outcome=registered
```

### AR-03 점검 미완 — `need_review`

```mermaid
sequenceDiagram
    autonumber
    participant E as 임직원
    participant A as FastAPI
    participant G as LangGraph
    participant D as PostgreSQL

    E->>A: POST /requests "노트북이 자꾸 꺼집니다"
    A->>D: INSERT request
    A->>G: invoke
    G->>D: load → inspection = pending
    Note over G: route_from_facts<br/>confirmed_faulty 아님 → need_review
    G->>D: state = needs_review
    G->>D: policy_refs = INSPECTION-REQUIRED
    A-->>E: "점검 상태가 pending 이다"
    Note over G,D: proposal 을 만들지 않는다.<br/>승인 대기 목록에 안 뜬다
```

### 대상 불명 — `need_info`

```mermaid
sequenceDiagram
    autonumber
    participant E as 임직원
    participant G as LangGraph
    participant L as OpenAI
    participant D as PostgreSQL

    E->>G: "지급받은 장비 하나가 고장났어요"
    G->>L: decide
    L-->>G: target_item_id = 2 (추론해서 고름)
    Note over G: verify_target<br/>요청문에 "독"·"노트북" 등이 없다<br/>→ 기각, target = null
    G->>D: state = needs_information
    Note over G: policy_refs = [] <br/>대상을 모르면 인용할 근거도 없다
```

### 귀책·비용 — `escalate`

```mermaid
sequenceDiagram
    autonumber
    participant E as 임직원
    participant G as LangGraph
    participant L as OpenAI
    participant D as PostgreSQL

    E->>G: "떨어뜨렸는데 제가 변상해야 하나요?"
    G->>L: decide
    L-->>G: is_liability_topic = true
    Note over G: 규칙 2 — 코드가 아니라<br/>LLM 이 판단하는 유일한 지점
    G->>D: state = escalated
    G->>D: policy_refs = COST-LIABILITY
```

---

## 4. 반복되는 루프 네 가지

```mermaid
flowchart LR
    subgraph L1 ["① 접수 재시도"]
        A1["POST /requests<br/>같은 Idempotency-Key"] --> A2{"intake_response<br/>있나"}
        A2 -->|있다| A3["저장된 응답 반환<br/>LLM 비용 0"]
        A2 -->|없다<br/>동시 도착| A4["UNIQUE 위반 → 409"]
    end

    subgraph L2 ["② 실행 재시도"]
        B1["POST /executions<br/>같은 request+version"] --> B2["execution_key 동일<br/>request_id:version:action"]
        B2 --> B3{"이미 있나"}
        B3 -->|있다| B4["digest 비교 후<br/>그대로 반환"]
        B3 -->|없다| B5["실제 실행"]
    end

    subgraph L3 ["③ 거절 후 재제출"]
        C1["rejected"] --> C2["새 proposal version"]
        C2 --> C3["awaiting_approval"]
        C3 --> C4["새 digest 로 재승인 필요"]
    end

    subgraph L4 ["④ 결과 불명 확인"]
        D1["outcome_unknown"] --> D2["resolve_outcome(execution_key)"]
        D2 --> D3{"request_execution<br/>에 기록 있나"}
        D3 -->|있다| D4["registered"]
        D3 -->|없다| D5["not_executed<br/>안전하게 재실행 가능"]
    end
```

③은 `proposal.version` 과 `payload_digest` 가 있어 성립한다. 처리안이 바뀌면 digest 가 달라지고, 옛 승인은 실행 단계에서 거부된다.

---

## 5. 실행 트랜잭션 — 검증 9단계

`execute_replacement` 는 이 순서를 지킨다. **순서가 전부다.**

```mermaid
flowchart TD
    S(["execute_replacement 시작"]) --> C1{"1. 실행자가<br/>IT 담당자인가"}
    C1 -->|아니다| X1["거부: 실행 권한이 없다<br/>audit_log"]
    C1 -->|맞다| C2{"2. 자산 주인 ==<br/>요청자인가"}
    C2 -->|아니다| X2["거부: 요청자에게<br/>지급된 자산이 아니다"]
    C2 -->|맞다| C3{"3. 같은 execution_key<br/>기록이 있나"}
    C3 -->|있다| R1["digest 같으면 그대로 반환<br/>다르면 거부"]
    C3 -->|없다| C4{"4. 유효한 승인이 있나<br/>can_approve 조인"}
    C4 -->|없다| X4["거부: 유효한 승인이 없다"]
    C4 -->|있다| C5{"5. 승인 digest ==<br/>현재 digest"}
    C5 -->|다르다| X5["거부: 승인 당시 내용과<br/>실행 내용이 다르다"]
    C5 -->|같다| C6{"6. 인용 규정 버전이<br/>아직 존재하나"}
    C6 -->|없다| X6["거부: 인용한 규정 버전이<br/>더 이상 존재하지 않는다"]
    C6 -->|있다| LK["7. FOR UPDATE 잠금<br/>assignment_item → asset_stock"]
    LK --> C7{"8. 잠근 뒤 재검증<br/>qty ≤ 미처리, qty ≤ 재고"}
    C7 -->|초과| X7["거부: 수량·재고 부족"]
    C7 -->|통과| W["9. 쓰기<br/>allocated_qty +qty<br/>available_qty -qty<br/>request_execution INSERT<br/>simulated_dispatch INSERT<br/>state = registered"]
    W --> E(["execution_key 반환"])

    classDef deny fill:#fdeeee,stroke:#c25b5b
    class X1,X2,X4,X5,X6,X7 deny
```

**잠금 순서가 고정이다** — `assignment_item` 다음 `asset_stock`. 뒤집으면 교착이 생긴다.

**잠근 뒤에 읽고 판단한다.** 잠그기 전 값으로 판정하면 잠금이 무의미하다(단계 32·33).

**함수가 커밋하지 않는다.** 트랜잭션 경계는 `get_conn` 이 정한다. 그래서 재고 차감·실행 기록·상태 갱신이 전부 되거나 전부 안 된다.

---

## 6. 두 개의 트랜잭션

관측이 업무와 분리돼 있다. 이 구조가 여러 곳에서 결과를 바꾼다.

```mermaid
flowchart LR
    subgraph T1 ["업무 트랜잭션 (get_conn)"]
        direction TB
        A["request INSERT"] --> B["proposal INSERT"]
        B --> C["state UPDATE"]
        C --> D{"예외"}
        D -->|있다| E["ROLLBACK<br/>전부 사라진다"]
        D -->|없다| F["COMMIT"]
    end

    subgraph T2 ["관측 트랜잭션 (engine)"]
        direction TB
        G["node_trace INSERT"] --> H["즉시 COMMIT"]
    end

    N["traced() 래퍼"] -.->|노드마다| T2
    N --> T1

    T2 --> I["업무가 롤백돼도<br/>기록은 남는다"]
```

| 얻은 것 | 치른 값 |
|---|---|
| 실패해도 "무엇을 시도했는지" 남는다 | `node_trace.request_id` 의 외래키를 끊어야 했다 |
| 평가 하니스가 롤백해도 비용·지연은 측정된다 | 시드 초기화 후 `request.id` 재사용으로 옛 기록이 섞인다 |

두 번째 대가가 실제로 청구됐다 — `request_id = 1` 에 서로 다른 실행이 7개 붙었고, 조회 쿼리에 `ORDER BY` 가 없어 아무거나 골라 화면에 띄웠다(단계 86·88).

---

## 7. API 7개와 화면

```mermaid
flowchart LR
    subgraph UI ["React 운영 화면"]
        T1["승인 대기 탭"]
        T2["실행 대기 탭"]
        T3["처리 과정"]
    end

    subgraph API ["FastAPI"]
        E1["GET /health"]
        E2["GET /employees/{id}/assignments"]
        E3["POST /requests"]
        E4["GET /proposals/pending?state="]
        E5["POST /proposals/{id}/approval"]
        E6["POST /executions"]
        E7["GET /requests/{id}/trace"]
    end

    T1 --> E4
    T1 --> E5
    T2 --> E4
    T2 --> E6
    T3 --> E7

    E4 -.->|awaiting_approval| T1
    E4 -.->|ready_to_execute| T2
```

`POST /executions` 의 본문은 `request_id` 와 `proposal_version` 뿐이다. **무엇을 몇 개 처리할지는 승인된 처리안에서 서버가 읽는다.** 화면은 "실행해라"만 말할 수 있고 "무엇을 몇 개"는 말할 수 없다(단계 84).

---

## 8. LLM 이 관여하는 지점

전체에서 LLM 호출은 **요청당 2회**다. 임베딩 1회, 판단 1회.

```mermaid
flowchart TD
    M["요청문"] --> R1["retrieve: 임베딩 1회<br/>text-embedding-3-small"]
    R1 --> R2["pgvector 코사인 거리<br/>top-3"]
    R2 --> P["프롬프트 조립"]
    M --> P
    AS["assignments (DB)"] --> P
    P --> D1["decide: LLM 1회<br/>gpt-4o-mini, t=0"]

    D1 --> O1["target_item_id"]
    D1 --> O2["qty"]
    D1 --> O3["is_liability_topic"]

    O1 --> VT["verify_target<br/>코드"]
    VT --> RFF["route_from_facts<br/>코드"]
    O2 --> RFF
    O3 --> RFF
    RFF --> RT["route + reason"]
    RT --> PPR["pick_policy_refs<br/>코드"]
    R2 --> PPR
    PPR --> CIT["policy_refs"]

    classDef llm fill:#fff4e6,stroke:#c2915b
    classDef code fill:#eef7ee,stroke:#5bc27c
    class R1,D1 llm
    class VT,RFF,PPR code
```

**LLM 출력 3개는 전부 코드 검증을 거친다.** 경로 이름도 근거 규정도 모델이 고르지 않는다.

| 누가 정하나 | 무엇 |
|---|---|
| LLM | 어떤 자산 · 몇 개 · 귀책 주제인가 |
| 코드 | 경로 · 설명 문구 · 인용 규정 · 수량 가능 여부 · 권한 |
| DB | 최종 거부 (CHECK · UNIQUE · FK · 행 잠금) |
