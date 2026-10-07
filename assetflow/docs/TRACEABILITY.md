# AssetFlow 검증 대응표

상태: 구현 및 로컬 검증 완료(2026-10-07). 전체 백엔드 스위트는 287개이며 pgvector
PostgreSQL Testcontainer에서 통과했다. 동시성 테스트 24개는 3회 연속 통과했다.
프런트엔드 lint와 production build도 통과했다. 현재 브랜치의 GitHub Actions 결과는
pull request, manual dispatch, or merge to `main` 이후 별도로 확인해야 하며, 로컬 결과를 CI 결과로 표기하지 않는다.

## 요구사항별 근거

| ID | 보장 조건 | 구현 근거 | 자동화 근거 |
|---|---|---|---|
| SYS-01 | 승인·예약 없는 실행 금지 | `services/execution.py` | `test_execution.py`, `test_execution_reservation.py` |
| SYS-02 | 처리안 버전·digest에 승인과 예약 결속 | approval/reservation 복합 FK와 실행 검증 | `test_reservation_schema.py`, `test_execution_reservation.py` |
| SYS-03 | 동일 실행 재시도는 한 번만 변경 | 서버 실행 키, 실행 원장 UNIQUE | `test_execution.py`, `test_execution_concurrency.py` |
| SYS-04 | 응답 유실 후 원장으로 결과 확정 | `GET /executions/status` | `test_execution_status.py`, `test_transaction_boundary.py` |
| SYS-05a | 배분 + 예약이 지급 수량을 넘지 않음 | item 잠금, CHECK, held 합계 | 승인·실행 예약 및 동시성 테스트 |
| SYS-05b | 비용 청구 실행 미노출 | 실행 원장 CHECK | `test_constraints.py` |
| SYS-06 | 재시작 후 DB 상태 유지 | PostgreSQL 업무 원장 | Docker 재기동 E2E(2026-10-06) |
| SYS-07 | 타인 자산 실행 금지 | 소유권 검증 | `test_execution.py`, `test_approval_reservation.py` |
| SYS-08a/b | 텍스트가 권한·실행 권한이 될 수 없음 | LLM과 승인·실행 경계 분리 | graph/target 검증 및 승인 권한 테스트; 전용 공격 stress셋은 후속 |
| SYS-09 | 동시 승인·실행에도 음수 재고 없음 | 고정 잠금 순서, held 예약 | 모든 `*_concurrency.py`, `tests/racing.py` |
| SYS-10 | 변경된 수량·재고를 잠금 아래 재검증 | execution 이중 예약 확인 | `test_execution_reservation.py` |
| SYS-11 | 같은 키에 다른 의도 거부 | digest 대조 | `test_execution.py` |
| SYS-12 | commit 전 실패 전체 rollback | 단일 트랜잭션·savepoint | 승인·실행·해제·재검토 원자성 테스트 |
| SYS-13 | 거절·철회 승인으로 실행 금지 | 활성 승인 필터 | `test_execution.py`, `test_execution_reservation.py` |
| SYS-14 | 귀책·비용 조건은 이관 | 결정적 route | `test_graph.py` |
| SYS-15 | 사라진 인용 정책으로 실행 금지 | 실행 시 정책 버전 대조 | `test_execution.py` |
| SYS-16 | DB 장애를 성공으로 오인하지 않음 | function-scope commit, 503, 원장 조회 | `test_transaction_boundary.py`, `test_execution_status.py` |
| SYS-17a/b | 승인 권한은 서버 DB가 판정 | `current_user`, `can_approve` | `test_api.py`, 승인·해제·재검토 테스트 |
| SYS-18 | 노드별 지연·토큰·비용·실패 기록 | `node_trace`, 관측 API | graph/smoke 테스트와 수동 화면 검증 |

## 예약 불변조건

| 불변조건 | 방어선 | 테스트 |
|---|---|---|
| 모델별 held 합계 ≤ `on_hand_qty` | 재고 행 잠금·승인 계산 | 승인/실행 동시성 테스트, `check_invariants` |
| `allocated_qty` + 항목별 held 합계 ≤ `qty` | 항목 행 잠금·승인 계산·CHECK | 승인/실행 동시성 테스트 |
| 승인과 예약은 함께 생성되거나 모두 없음 | 한 트랜잭션 | `test_approval_reservation.py` |
| 소비된 예약당 모의 지급 정확히 1건 | 실행 원장과 FK·UNIQUE | `test_execution_reservation.py`, `check_invariants` |
| 철회 승인·해제 예약은 실행 불가 | 활성 승인·held 필터 | 실행/해제 테스트 |
| 실행과 해제 경합은 하나만 성공 | 동일 잠금 순서 | `test_release_concurrency.py` |
| 재검토는 실행된 처리안을 되돌리지 않음 | 실행 원장 선검사 | `test_re_review.py`, 동시성 테스트 |

## 2026-10-06 전체 검증

- `pytest -q`: 262 passed, LangGraph pending-deprecation 경고 1건(당시 기준. 현재 287개는 아래 2026-10-07 보완 참고).
- 동시성 묶음 24개: 3회 연속 통과.
- `npm run lint`, `npm run build`: 통과.
- 별도 빈 Compose 볼륨: migration, seed, db/api/web healthcheck 통과.
- HTTP E2E: 승인 → 수량 부족 409 → 예약 해제 → 재검토 → 재승인 → 실행 → 동일 키 재시도 → 원장 조회 통과.
- 권한 없는 승인·해제·재검토: 403.
- DB 불변조건 조회: 세 위반 항목 모두 0; consumed/execution/dispatch/registered 각 1건.
- 컨테이너 재기동 후 실행 결과 유지, 오류 로그 없음.

## 2026-10-07 보완

- 조회 API도 `current_user` 데모 인증으로 주체를 확인한다. 요청자 이름·보유 자산·처리 내용이 담기기 때문이다.
  - `GET /proposals/pending`, `GET /requests/{id}/trace`, `GET /observability/runs`, `GET /observability/runs/{run_id}`: IT 담당자만.
  - `GET /employees/{id}/assignments`: 본인 또는 IT 담당자.
  - 헤더 없음 422, 미등록 사용자 401, 권한 없음 403, 승인 권한 없는 담당자는 조회 가능(`test_pending_quantities.py`, `test_read_authorization.py`).
- `pytest -q`: 287 passed(262 → 266: `test_pending_quantities.py` 4개 추가, 266 → 287: `test_read_authorization.py` 21개 추가). `npm run lint`, `npm run build`: 통과.
- 원격 CI: 아직 실행되지 않음. 위 결과는 모두 로컬 실행이다.

## 아직 주장하지 않는 것

- 전용 prompt-injection stress셋의 최종 결과
- pull request·manual dispatch·`main` 반영 전 현재 287개 스위트의 GitHub Actions 통과
- 공개 클라우드 배포, 실제 사용자·재고·지급 데이터
- 외부 지급 시스템을 포함한 exactly-once 실행
- 자동 예약 만료, 고가용성, 부하·SLO 검증
