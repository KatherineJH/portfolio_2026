# AssetFlow

[![AssetFlow Quality Gate](https://github.com/KatherineJH/portfolio_2026/actions/workflows/assetflow-quality.yml/badge.svg)](https://github.com/KatherineJH/portfolio_2026/actions/workflows/assetflow-quality.yml)

AssetFlow is an evidence-based workflow for internal IT asset requests. Employees describe a replacement request in natural language, the service checks assignment and inspection facts, retrieves relevant policy, and creates a bounded action proposal. An authorized IT operator must approve that proposal before the system can register a simulated dispatch.

The project demonstrates an agentic workflow rather than an unrestricted chatbot. The LLM extracts a target asset, quantity, and liability topic; ordinary Python code and PostgreSQL remain authoritative for routing, authorization, inventory, approvals, and execution.

All people, policies, assets, and requests are synthetic. Dispatch is simulated and has no external side effect.

## Product flow

1. An employee selects a demo persona and submits an asset request through chat.
2. LangGraph loads the employee's assignments and policy candidates.
3. `gpt-4o-mini` extracts a small set of facts from the request.
4. Server rules choose one route: request information, require review, escalate, or propose an action.
5. The response explains the reason and shows a versioned policy reference.
6. An IT operator reviews the proposal; approval permission is checked from the database.
7. Approval atomically creates a version-bound reservation for stock and the employee's remaining assignment quantity.
8. Execution revalidates the reservation and converts it from `held` to `consumed` while updating allocation and inventory.
9. Operators can release an unconsumed reservation, re-review the request, and approve it again without deleting history.

## Interface

The React application has three workspaces:

- **Request intake** — employee personas (`kim`, `lee`, `park`), real LLM calls, route-specific responses, and cited policies.
- **Approval management** — approval, reserved/on-hand quantity display, execution, reservation release, and re-review.
- **Workflow observability** — run-level latency, token use, estimated cost, outcome, and node timelines.

Persona switching is a demo feature, not authentication. The browser sends `x-user-id` to reproduce scenarios. A production service would validate a signed token or session and derive a trusted user identity in the backend.

## Safety boundaries

- **Evidence is not authority.** Policy text supports a proposal; an LLM response cannot approve or execute it.
- **Approval is version-bound.** Approval stores the proposal version and payload digest reviewed by the operator.
- **Approval reserves quantity.** Approval and its `held` stock/assignment reservation commit together.
- **Authorization is server-owned.** The API reads `can_approve` from PostgreSQL. Read APIs also resolve the demo identity: assignment history is visible to the employee or an IT operator, and pending proposals, request traces, and run observability are limited to IT operators.
- **Execution is idempotent.** An execution key prevents duplicate simulated dispatches.
- **Locks have one order.** Mutation paths lock request, assignment item, stock, then reservation as needed.
- **Success follows commit.** Database commit completes before an HTTP success response is sent.
- **Observability is first-class.** Nodes record latency, model usage, estimated cost, result, and failure information.

See [ADR-001](docs/adr/001-claim-execution.md) for the execution identity and transaction boundary, and [ADR-002](docs/adr/002-stock-reservation.md) for reservation, release, and re-review decisions.

## Architecture

```text
React + TypeScript
        |
        | /api (Vite proxy)
        v
FastAPI :8005
        |
        +-- LangGraph workflow
        +-- OpenAI chat and embeddings
        +-- server validation and authorization
        |
        v
PostgreSQL :5433 + pgvector
```

PostgreSQL is authoritative for users, assignments, inspections, inventory, requests, proposals, approvals, executions, simulated dispatches, and traces. LangGraph state is transient workflow state, not the business source of truth.

### Legacy database compatibility

The portfolio UI can read an older local AssetFlow database containing policy records but no policy chunks or later intake columns. In compatibility mode, the database is not migrated or modified. Policy records are loaded as a small candidate set, policy keys are mapped to server-owned subjects, and the deterministic route selects the final citation. Persisted intake-key deduplication is unavailable because the legacy columns do not exist.

This fallback is not presented as vector retrieval. A fresh database created from the current migrations uses policy chunks and pgvector.

## Measured results

The frozen evaluation used `gpt-4o-mini` at temperature 0 and `text-embedding-3-small`. Each held-out case was repeated three times.

| Metric | Development set (8) | Held-out set (16) |
|---|---:|---:|
| Route accuracy | 1.00 | **0.88** |
| Target asset accuracy | 1.00 | 0.88 |
| Policy citation accuracy | 1.00 | 0.81 |

| Runtime metric | Measured value |
|---|---:|
| LLM cost per request | approximately $0.000227 |
| LLM-node latency p95 | 1,544 ms |
| Database-node latency p95 | under 5 ms |
| Automated tests | 287 passed |

The held-out set contains 16 synthetic cases and was authored within the same project. These results demonstrate reproducibility on the supplied scenarios, not production generalization. See [Evaluation Results](docs/EVALUATION-RESULTS.md) and [experiment history](analysis/experiments.csv).

## Local setup

Requirements: Docker Desktop, Python 3.11+, [uv](https://docs.astral.sh/uv/), Node.js 20+, and an OpenAI API key.

### 1. PostgreSQL

Create `infra/.env`:

```dotenv
POSTGRES_PASSWORD=localdev
```

```powershell
cd infra
docker compose up -d db
docker compose ps
```

The database is exposed at `127.0.0.1:5433`.

### 2. FastAPI

```powershell
cd ai-service
uv venv .venv
uv pip install --python .\.venv\Scripts\python.exe -r requirements.txt -r requirements-dev.txt
Copy-Item .env.example .env
```

Set `ai-service/.env`:

```dotenv
POSTGRES_PASSWORD=localdev
DATABASE_URL=postgresql+psycopg://assetflow:localdev@localhost:5433/assetflow
OPENAI_API_KEY=your_key_here
```

For a new, empty database:

```powershell
uv run alembic upgrade head
uv run python seed_dev.py
uv run python embed_policies.py
```

Start the API:

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8005
```

Do not run migrations or seed commands against an older AssetFlow database whose Alembic history is not included in this repository. Runtime compatibility can read that database without changing it.

### 3. React

```powershell
cd frontend
npm ci
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/api` to `http://127.0.0.1:8005`.

### Full stack in containers

Steps 2 and 3 can be replaced by one Compose stack: PostgreSQL, a one-shot Alembic migration, the API, and the web UI behind nginx. Services start in dependency order and wait on healthchecks.

```powershell
cd infra
# infra/.env needs POSTGRES_PASSWORD; OPENAI_API_KEY is read from the environment.
docker compose up -d --build --wait
docker compose --profile tools run --rm seed   # first run only
```

Open `http://127.0.0.1:8080`. nginx forwards `/api` to the API container, matching the Vite proxy rule. All ports bind to `127.0.0.1`. Do not run `seed` against a database whose Alembic history is not in this repository.

## Verification

The repository includes an [AssetFlow Quality Gate](../.github/workflows/assetflow-quality.yml) that runs on changes under `assetflow/**`. It executes the backend test suite in a pgvector Testcontainer and runs frontend lint plus the production build. A Docker job then builds both images, starts the Compose stack, smoke-tests it through the web proxy, and scans the images for fixed HIGH and CRITICAL vulnerabilities. Two accepted findings are listed with reasons in [`.trivyignore`](.trivyignore). The job builds and verifies images; nothing is deployed. See [Operations Evidence](docs/OPERATIONS-EVIDENCE.md) for the distinction between automated, local, manual, and not-yet-verified evidence.

Backend tests use an isolated pgvector PostgreSQL through Testcontainers, not the demo database.

```powershell
cd ai-service
uv run pytest -q
```

Verified locally on 2026-10-07: `287 passed` with one non-blocking LangGraph pending-deprecation warning. The 24 concurrency tests passed three consecutive runs on 2026-10-06.

```powershell
cd frontend
npm run build
```

Verified locally on 2026-10-06: ESLint, TypeScript compilation, and the Vite production build completed successfully.

The 2026-10-06 isolated Docker verification used a new Compose project and empty volume. It covered:

- migration, seed, and healthy database/API/web containers;
- successful approval with reservation and a competing approval refused for insufficient assignment quantity;
- reservation release, re-review, re-approval, and execution;
- authorization failures for approval, release, and re-review;
- identical execution retry returning the existing result;
- execution-ledger status lookup and persistence after container restart; and
- zero violations for held-stock, assignment-capacity, and consumed-dispatch invariants.

This Docker run did not call OpenAI. The earlier measured evaluation remains the evidence for LLM routing and citation behavior. The current feature branch needs a pull request or manual workflow dispatch (or must reach `main`) before its GitHub Actions result can be recorded as CI-verified.

## Repository guide

- [`ai-service/app`](ai-service/app) — API, workflow, retrieval, execution, and tracing
- [`ai-service/migrations`](ai-service/migrations) — Alembic schema history
- [`ai-service/tests`](ai-service/tests) — API, constraint, concurrency, execution, graph, and authorization tests
- [`frontend/src`](frontend/src) — React application and API client
- [`docs/SCENARIOS.md`](docs/SCENARIOS.md) — scenarios and decision rules
- [`docs/TRACEABILITY.md`](docs/TRACEABILITY.md) — verification map
- [`docs/SCHEMA.md`](docs/SCHEMA.md) — schema and transaction design
- [`docs/EVALUATION.md`](docs/EVALUATION.md) — evaluation protocol
- [`docs/EVALUATION-RESULTS.md`](docs/EVALUATION-RESULTS.md) — measurements and limitations
- [`docs/OPERATIONS-EVIDENCE.md`](docs/OPERATIONS-EVIDENCE.md) — CI, local runtime, and deployment evidence
- [`docs/adr/001-claim-execution.md`](docs/adr/001-claim-execution.md) — approval and execution ADR
- [`docs/adr/002-stock-reservation.md`](docs/adr/002-stock-reservation.md) — stock reservation, release, and re-review ADR

## Limitations

- Authentication is simulated with selectable personas and `x-user-id`.
- All business data and policies are synthetic.
- Dispatch is simulated.
- Reservations have no automatic expiry; an authorized operator releases them manually.
- Creating a replacement proposal version is not exposed yet.
- Legacy compatibility does not provide persisted intake-key deduplication or vector retrieval when chunks are absent.
- The evaluation is small and domain-specific.
- The application is verified locally and is not presented as production infrastructure.

## AI-assisted development disclosure

AI tools assisted with planning, implementation drafts, review, and documentation. Verified artifacts include schema constraints, transaction behavior, authorization checks, scenario tests, measured evaluation, the production frontend build, and the manually exercised end-to-end workflow.
