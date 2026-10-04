# AssetFlow 최소 스키마 설계

그림과 한국어 용어 설명은 [SCHEMA-ERD.md](SCHEMA-ERD.md)에 있다. 이 문서의 DDL이 단일 출처다.

상태: 설계. 마이그레이션·앱 코드는 아직 없다. 근거는 [ADR-001](adr/001-claim-execution.md)(Accepted, 2026-09-09)이며 각 제약은 [TRACEABILITY.md](TRACEABILITY.md)의 SYS 항목과 연결한다.

범위는 최소판이다. 점검 완료된 자산의 교체 요청 처리, 조회·추가 질문·이관, 승인, 모의 지급 등록, 노드별 관측까지만 다룬다. 비용 청구는 초안·이관까지이며 실행 경로를 만들지 않는다.

## 설계 원칙

- PostgreSQL 업무 레코드가 권위다. LangGraph 체크포인트는 이 레코드를 참조하며 실행 증거로 쓰지 않는다.
- 승인 대기 중 재고를 예약하지 않는다. 실행 트랜잭션에서 원자적으로 배분한다.
- 수량·금액·권한은 DB 제약과 결정적 코드가 판정한다. LLM 출력은 판정 근거가 아니다.
- 잠금 순서는 항상 `assignment_item` → `asset_stock`이다. 교착을 피하기 위해 역순으로 잠그지 않는다.
- 규정 검색은 같은 PostgreSQL 안에서 pgvector로 처리한다. 별도 벡터 저장소를 추가하지 않는다.
- 관측은 부가 기능이 아니라 필수 산출물이다. 성공한 실행과 실패한 실행 모두 트레이스를 남긴다(SYS-18).

## 테이블

### 주체와 권한

```sql
CREATE TYPE user_kind AS ENUM ('employee', 'it_operator');

CREATE TABLE app_user (
  id           bigserial PRIMARY KEY,
  kind         user_kind   NOT NULL,
  display_name text        NOT NULL,
  can_approve  boolean     NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT employee_cannot_approve
    CHECK (kind = 'it_operator' OR can_approve = false)
);
```

`can_approve`는 서버가 확인하는 승인 권한이다. 인증 성공만으로 승인할 수 없고, 요청 본문이 제공한 역할·승인자 ID는 권한 근거로 쓰지 않는다 (SYS-17a/17b).

### 자산과 재고

```sql
CREATE TYPE asset_category AS ENUM ('LAPTOP', 'PERIPHERAL', 'MOBILE');

CREATE TABLE asset_model (
  id       bigserial PRIMARY KEY,
  code     text NOT NULL UNIQUE,
  name     text NOT NULL,
  category asset_category NOT NULL
);

CREATE TABLE asset_stock (
  asset_model_id bigint PRIMARY KEY REFERENCES asset_model(id),
  available_qty  integer NOT NULL,
  CONSTRAINT stock_not_negative CHECK (available_qty >= 0)
);
```

`stock_not_negative`가 동시 실행에서 음수 재고를 차단한다 (SYS-09).

### 지급 이력

```sql
CREATE TABLE assignment (
  id          bigserial PRIMARY KEY,
  employee_id bigint      NOT NULL REFERENCES app_user(id),
  status      text        NOT NULL,
  assigned_at timestamptz NOT NULL
);

CREATE TABLE assignment_item (
  id                bigserial PRIMARY KEY,
  assignment_id     bigint  NOT NULL REFERENCES assignment(id),
  asset_model_id    bigint  NOT NULL REFERENCES asset_model(id),
  qty               integer NOT NULL,
  unit_acquired_cost numeric(12,2) NOT NULL,
  handover_status   text    NOT NULL,
  allocated_qty     integer NOT NULL DEFAULT 0,
  CONSTRAINT qty_positive      CHECK (qty > 0),
  CONSTRAINT cost_not_negative CHECK (unit_acquired_cost >= 0),
  CONSTRAINT allocation_within_qty
    CHECK (allocated_qty >= 0 AND allocated_qty <= qty)
);
```

미처리 수량은 `qty - allocated_qty`로 계산한다. `allocation_within_qty`가 DB 차원의 상한이므로, 서로 다른 실행 키나 서로 다른 request로 우회해도 총 배분이 지급 수량을 넘지 못한다 (SYS-05a).

### 점검 상태

```sql
CREATE TYPE inspection_status AS ENUM ('pending', 'confirmed_faulty', 'rejected');

CREATE TABLE inspection (
  assignment_item_id bigint PRIMARY KEY REFERENCES assignment_item(id),
  status             inspection_status NOT NULL DEFAULT 'pending',
  inspected_by       bigint REFERENCES app_user(id),
  inspected_at       timestamptz,
  CONSTRAINT confirmed_requires_inspector
    CHECK (status = 'pending' OR (inspected_by IS NOT NULL AND inspected_at IS NOT NULL))
);
```

IT 담당자가 점검한 상태값이며 이미지 모델 판독 결과가 아니다. 요청자 진술만 있으면 `pending`으로 남고 request는 `needs_review`가 된다 (AR-03).

### 규정과 벡터 검색

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE policy (
  id             bigserial PRIMARY KEY,
  policy_key     text    NOT NULL,
  version        integer NOT NULL,
  effective_from date    NOT NULL,
  condition_text text    NOT NULL,
  exception_text text,
  required_info  text,
  allowed_action text    NOT NULL,
  is_fictional   boolean NOT NULL DEFAULT true,
  UNIQUE (policy_key, version)
);

CREATE TABLE policy_chunk (
  id            bigserial PRIMARY KEY,
  policy_id     bigint  NOT NULL REFERENCES policy(id),
  chunk_index   integer NOT NULL,
  content       text    NOT NULL,
  embedding     vector(1536) NOT NULL,
  embed_model   text    NOT NULL,
  UNIQUE (policy_id, chunk_index)
);

CREATE INDEX policy_chunk_embedding_idx
  ON policy_chunk USING hnsw (embedding vector_cosine_ops);
```

`embed_model`을 함께 저장한다. 임베딩 모델을 바꾸면 벡터를 재생성해야 하므로, 평가 결과가 어떤 모델로 만든 인덱스에서 나왔는지 기록에 남아야 한다.

가상 사내 규정임을 `is_fictional`로 표시한다. 법적 준수를 주장하지 않는다.

### 요청 사례와 처리안

```sql
CREATE TYPE request_state AS ENUM (
  'needs_information', 'needs_review', 'awaiting_approval',
  'rejected', 'ready_to_execute', 'outcome_unknown',
  'registered', 'escalated'
);

CREATE TABLE request (
  id            bigserial PRIMARY KEY,
  employee_id   bigint        NOT NULL REFERENCES app_user(id),
  assignment_id bigint        REFERENCES assignment(id),
  state         request_state NOT NULL DEFAULT 'needs_information',
  created_at    timestamptz   NOT NULL DEFAULT now(),
  updated_at    timestamptz   NOT NULL DEFAULT now()
);

CREATE TYPE action_type AS ENUM ('replacement', 'cost_claim_draft', 'escalate');

CREATE TABLE proposal (
  id             bigserial PRIMARY KEY,
  request_id     bigint      NOT NULL REFERENCES request(id),
  version        integer     NOT NULL,
  action_type    action_type NOT NULL,
  payload        jsonb       NOT NULL,
  payload_digest text        NOT NULL,
  policy_refs    jsonb       NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (request_id, version)
);
```

`proposal`은 불변이다. 수량이나 자산이 바뀌면 새 `version`을 만든다. `policy_refs`에 인용한 `policy_key`/`version` 목록을 남겨 승인 이후 규정 변경을 감지한다 (SYS-15). 근거를 찾지 못하면 `escalate`로 남기고 처리안을 만들지 않는다.

`request_id`는 요청 사례를 식별하며 모델 실행 단위가 아니다. 같은 요청이 반복 도착해도 그 자체로 실행 권한이 되지 않는다.

### 승인

```sql
CREATE TYPE approval_decision AS ENUM ('approve', 'reject');

CREATE TABLE approval (
  id               bigserial PRIMARY KEY,
  request_id       bigint  NOT NULL REFERENCES request(id),
  proposal_id      bigint  NOT NULL REFERENCES proposal(id),
  proposal_version integer NOT NULL,
  payload_digest   text    NOT NULL,
  approver_id      bigint  NOT NULL REFERENCES app_user(id),
  decision         approval_decision NOT NULL,
  decided_at       timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX one_active_approval_per_proposal
  ON approval (proposal_id) WHERE decision = 'approve';
```

승인은 특정 처리안 버전과 그 payload 다이제스트에 결속된다. v1 승인으로 v2를 실행할 수 없다 (SYS-02). 거절 레코드는 남지만 등록을 만들지 않는다 (SYS-13). 승인 API는 `approver_id`를 본문에서 받지 않고 서버가 확인한 주체로 채운다.

### 실행과 멱등성

```sql
CREATE TYPE execution_outcome AS ENUM ('registered', 'rejected', 'unknown');

CREATE TABLE request_execution (
  execution_key    text PRIMARY KEY,
  request_id       bigint      NOT NULL REFERENCES request(id),
  proposal_version integer     NOT NULL,
  action_type      action_type NOT NULL,
  payload_digest   text        NOT NULL,
  outcome          execution_outcome NOT NULL,
  error_reason     text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT minimum_scope_blocks_cost_claim_execution
    CHECK (action_type = 'replacement'),
  UNIQUE (request_id, proposal_version, action_type)
);

CREATE TABLE simulated_dispatch (
  id                 bigserial PRIMARY KEY,
  execution_key      text    NOT NULL UNIQUE REFERENCES request_execution(execution_key),
  assignment_item_id bigint  NOT NULL REFERENCES assignment_item(id),
  qty                integer NOT NULL,
  registered_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT dispatch_qty_positive CHECK (qty > 0)
);
```

`execution_key`는 서버가 `request_id`/`proposal_version`/`action`에 대해 발급하며 재시도 시 재사용한다. 같은 키에 같은 payload면 기존 결과를 반환하고, 같은 키에 다른 payload면 충돌로 거부한다 (SYS-03, SYS-11). `simulated_dispatch.execution_key`의 UNIQUE가 등록 중복을 차단한다.

`minimum_scope_blocks_cost_claim_execution`이 최소판에서 비용 청구 실행을 DB 차원에서 막는다. 확장 게이트를 통과하고 SYS-05b가 실행되기 전까지 이 CHECK를 완화하지 않는다.

등록 완료는 모의 지급이며 실물 전달 완료가 아니다.

### 관측

```sql
CREATE TABLE node_trace (
  id                bigserial PRIMARY KEY,
  run_id            uuid    NOT NULL,
  request_id        bigint,   -- 느슨한 참조. 외래키 없음 (별도 트랜잭션 기록)
  node_name         text    NOT NULL,
  attempt_no        integer NOT NULL DEFAULT 1,
  started_at        timestamptz NOT NULL,
  ended_at          timestamptz,
  latency_ms        integer,
  input_summary     jsonb,
  output_summary    jsonb,
  model             text,
  prompt_tokens     integer,
  completion_tokens integer,
  cost_usd          numeric(10,6),
  outcome           text    NOT NULL,
  error_reason      text,
  CONSTRAINT trace_outcome_values
    CHECK (outcome IN ('ok', 'retried', 'failed')),
  CONSTRAINT trace_attempt_positive CHECK (attempt_no >= 1)
);

CREATE INDEX node_trace_run_idx ON node_trace (run_id, started_at);
```

`run_id`로 한 번의 워크플로 실행을 묶는다. 실패한 노드도 행을 남기며 `error_reason`을 채운다. 실패 건에 트레이스가 없으면 SYS-18이 통과하지 않는다.

`input_summary`/`output_summary`는 요약만 저장한다. 전체 프롬프트와 응답 원문은 저장하지 않는다 — 개인정보와 저장 비용 때문이며, 재현이 필요한 경우 fixture 버전과 프롬프트 버전으로 복원한다.

`request_id` 에 외래키를 두지 않는다. 
트레이스를 별도 트랜잭션에서 커밋하므로 아직 커밋되지 않은 `request` 를 참조할 수 없고, 
참조 무결성보다 기록이 남는 것이 우선이기 때문이다. 조회를 위해 인덱스만 둔다.

### 감사

```sql
CREATE TABLE audit_log (
  id                 bigserial PRIMARY KEY,
  actor_id           bigint REFERENCES app_user(id),
  actor_role_claimed text,
  action             text NOT NULL,
  target_type        text NOT NULL,
  target_id          text,
  result             text NOT NULL,
  reason             text,
  at                 timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT result_values CHECK (result IN ('allowed', 'denied'))
);
```

거부도 기록한다. `actor_role_claimed`는 클라이언트가 주장한 역할을 그대로 보존해 위조 시도를 남기며, 권한 판정에는 사용하지 않는다 (SYS-17b).

## 실행 트랜잭션 순서

단일 트랜잭션에서 아래를 수행하고 함께 커밋한다.

1. 인증 주체 확인. 지급 소유 관계와 `can_approve` 확인. 실패 시 `audit_log`에 `denied` 기록 후 중단.
2. `request_execution`을 `execution_key`로 조회. 같은 payload 다이제스트면 기존 결과 반환. 다른 다이제스트면 충돌 거부.
3. 유효 승인 조회. `decision = 'approve'`, `proposal_version`이 현재 최신 버전, `payload_digest` 일치.
4. `proposal.policy_refs`와 현재 규정 버전 비교. 불일치면 재검토 상태로 전환하고 실행하지 않는다.
5. `SELECT ... FOR UPDATE`로 `assignment_item`을 잠그고, 이어서 `asset_stock`을 잠근다.
6. `qty - allocated_qty >= 요청 수량`, `available_qty >= 요청 수량` 확인. 실패 시 거부하거나 대체 제시로 되돌린다.
7. `allocated_qty` 증가, `available_qty` 감소.
8. `request_execution` 삽입, `simulated_dispatch` 삽입, `request.state = 'registered'` 갱신.
9. 커밋.

트레이스는 이 트랜잭션과 분리해 기록한다. 관측 기록 실패가 업무 실행을 롤백시키면 안 되고, 반대로 트레이스를 업무 성공의 증거로 쓰지도 않는다.

커밋 후 응답이 유실되면 같은 `execution_key`로 조회해 기존 결과를 확인한다. 새 키로 두 번째 변경을 만들지 않는다. 커밋 전 오류는 전체 롤백되며 같은 키로 재검증 후 재시도할 수 있다. DB에 접근할 수 없으면 `outcome_unknown`을 유지하고 성공·실패를 단정하지 않는다.

## SYS 검증 대응

| SYS | 스키마에서 확인하는 지점 |
|---|---|
| SYS-01 | 실행 트랜잭션 3단계. `approval`에 승인 레코드가 없으면 삽입·갱신 0건 |
| SYS-02 | `approval.proposal_version` + `payload_digest`와 현재 `proposal` 대조 |
| SYS-03 | `request_execution.execution_key` PK, `simulated_dispatch.execution_key` UNIQUE |
| SYS-04 | 커밋된 `request_execution` 조회로 기존 등록 발견 |
| SYS-05a | `assignment_item.allocation_within_qty` CHECK |
| SYS-05b | `request_execution.minimum_scope_blocks_cost_claim_execution` CHECK |
| SYS-06 | `request.state = 'awaiting_approval'`과 `proposal.version` 복원 |
| SYS-07 | `assignment.employee_id` 소유 확인 + `audit_log` 거부 기록 |
| SYS-08a/08b | 권한·승인 판정이 `app_user.can_approve`와 `approval`에만 의존 |
| SYS-09 | `asset_stock.stock_not_negative` CHECK + 행 잠금 |
| SYS-10 | 실행 6단계 재검증 실패 → 등록 없음 |
| SYS-11 | 같은 `execution_key`에 다른 `payload_digest` → 충돌 거부 |
| SYS-12 | 단일 트랜잭션 롤백 후 같은 키 재시도 |
| SYS-13 | `decision = 'reject'`면 `one_active_approval_per_proposal` 부재로 실행 불가 |
| SYS-14 | `request.state = 'escalated'`, `action_type = 'cost_claim_draft'`까지만 |
| SYS-15 | `proposal.policy_refs`와 현재 `policy.version` 비교 |
| SYS-16 | `request_execution.outcome = 'unknown'` 유지 |
| SYS-17a/17b | `app_user.can_approve` 서버 확인 + `audit_log.actor_role_claimed` 보존 |
| SYS-18 | `node_trace`에 성공·실패 실행의 노드별 행이 존재하고 지연·토큰·비용·`attempt_no`가 채워짐 |

## 다음 작업

- 마이그레이션 작성 (Alembic) 및 위 제약이 실제로 거부하는지 확인하는 제약 테스트.
- fixture: 자산 모델 5~10개, 지급 이력 약 20건, 규정 10~15개 조항, 고정 시드. 정상 데이터와 의도적 모순 데이터를 분리한다.
- 미결: `request.state` 전이 규칙을 코드로 강제할지, 애플리케이션 계층에만 둘지. 최소판은 애플리케이션 계층으로 시작하고 필요 시 트리거를 검토한다.
- 미결: `policy_chunk.embedding` 차원은 임베딩 모델 선택 후 확정한다. 현재 1536은 OpenAI `text-embedding-3-small` 기준 가정이다.
