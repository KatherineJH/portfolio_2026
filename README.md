# portfolio_2026

[![AssetFlow Quality Gate](https://github.com/KatherineJH/portfolio_2026/actions/workflows/assetflow-quality.yml/badge.svg)](https://github.com/KatherineJH/portfolio_2026/actions/workflows/assetflow-quality.yml)

2026년 개인 프로젝트 포트폴리오 저장소입니다. 현재 **AssetFlow** 프로젝트 하나를 담고 있습니다.

## AssetFlow

> 잘 대답하는 AI보다 잘못 실행하지 않는 AI

사내 IT 자산 교체 요청을 처리하는 Agentic AI 워크플로입니다. 직원이 자연어로 요청하면 서비스가 지급·점검 이력과 관련 규정을 조회해 처리안을 만들고, 권한 있는 IT 담당자가 승인한 뒤에만 (모의) 지급이 실행됩니다.

LLM은 요청에서 대상 자산·수량·귀책 주제를 추출하는 역할로 제한하고, 처리 경로·권한·재고·승인·실행은 Python 코드와 PostgreSQL이 결정하도록 설계했습니다.

- **스택**: FastAPI · LangGraph · React + TypeScript · PostgreSQL + pgvector · Docker Compose
- **범위**: 개인 프로젝트. 사람·규정·자산·요청 데이터는 모두 합성 데이터이며, 지급은 외부 부작용이 없는 시뮬레이션입니다.

주요 설계 포인트:

- 승인은 처리안 버전과 payload digest에 결속되고, 재고·지급 가능 수량 예약과 함께 커밋됩니다.
- 승인 권한은 브라우저가 아니라 DB에서 확인합니다.
- 실행 키로 중복 실행을 막고, 잠금 순서를 고정해 동시 요청을 처리합니다.
- 노드별 지연 시간·토큰 사용량·추정 비용을 기록하는 관측 화면을 제공합니다.

평가 결과(합성 평가셋 기준)와 한계는 [Evaluation Results](assetflow/docs/EVALUATION-RESULTS.md)와 [AssetFlow README](assetflow/README.md#limitations)에 정리했습니다. 수치는 프로젝트 안에서 작성한 소규모 시나리오에 대한 재현 결과이며, 운영 환경의 일반화 성능을 의미하지 않습니다.

## 저장소 구조

```text
portfolio_2026/
├── .github/workflows/
│   └── assetflow-quality.yml   # AssetFlow 품질 게이트 (pytest · lint/build · Docker smoke/스캔)
└── assetflow/
    ├── ai-service/             # FastAPI API, LangGraph 워크플로, Alembic 마이그레이션, 테스트, 평가 스크립트
    ├── frontend/               # React + TypeScript (요청 · 승인 관리 · 워크플로 관측 화면)
    ├── infra/                  # Docker Compose (db · migrate · api · web · seed)
    ├── data/                   # 개발용 / 최종(held-out) 평가 시나리오
    ├── analysis/               # 실험 기록 (experiments.csv)
    ├── docs/                   # 시나리오, 스키마, 평가, 운영 증거, ADR
    └── portfolio/              # 프로젝트 소개용 정적 페이지 (index.html)
```

## 보는 방법

- **프로젝트 문서**: [assetflow/README.md](assetflow/README.md) — 제품 흐름, 아키텍처, 안전 경계, 로컬 실행 방법, 검증 내역
- **소개 페이지**: [assetflow/portfolio/index.html](assetflow/portfolio/index.html) — 별도 빌드 없이 브라우저에서 파일을 직접 열면 됩니다.
- **설계 결정**: [ADR-001 실행 식별자와 트랜잭션 경계](assetflow/docs/adr/001-claim-execution.md), [ADR-002 재고 예약·해제·재검토](assetflow/docs/adr/002-stock-reservation.md)

## 실행 방법

Docker Desktop과 OpenAI API 키가 필요합니다. 컨테이너로 전체 스택을 띄우는 방법은 다음과 같습니다.

```powershell
cd assetflow/infra
# infra/.env 에 POSTGRES_PASSWORD 를 설정하고, OPENAI_API_KEY 는 환경 변수로 전달합니다.
docker compose up -d --build --wait
docker compose --profile tools run --rm seed   # 최초 1회
```

`http://127.0.0.1:8080`에서 열립니다. API·프론트엔드를 따로 실행하는 방법과 테스트 실행 방법은 [AssetFlow README의 Local setup](assetflow/README.md#local-setup)을 참고하세요.

## AI 활용 고지

기획, 구현 초안, 리뷰, 문서화에 AI 도구를 활용했습니다. 자세한 내용은 [AssetFlow README](assetflow/README.md#ai-assisted-development-disclosure)에 있습니다.
