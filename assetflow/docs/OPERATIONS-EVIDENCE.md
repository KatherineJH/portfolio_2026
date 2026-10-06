# AssetFlow Operations Evidence

This document separates reproducible evidence from planned or unverified production claims. AssetFlow is a locally verified portfolio MVP, not a production deployment.

## Evidence matrix

| Area | Evidence | Status | Scope boundary |
|---|---|---|---|
| Backend quality gate | `pytest -q` → `262 passed` on 2026-10-06 | Locally verified | Uses a pgvector PostgreSQL Testcontainer; no OpenAI call; one non-blocking LangGraph warning |
| Concurrency regression | 24 concurrency tests passed three consecutive runs on 2026-10-06 | Locally verified | PostgreSQL row locks and forced overlap; not a throughput benchmark |
| Frontend static analysis | `npm run lint` completed with no errors on 2026-10-06 | Locally verified | ESLint only; no browser automation suite |
| Frontend production build | TypeScript and Vite production build completed on 2026-10-06 | Locally verified | Build success is not a public deployment |
| CI quality gate | [`6d12b6d`](https://github.com/KatherineJH/portfolio_2026/commit/6d12b6d96abb743f9c79603113f35eb9fa48bd0f/checks) — run #2 completed in 29 seconds; backend and frontend jobs succeeded | GitHub-verified | Evidence is tied to the tested commit; branch protection is not enabled |
| Full-stack runtime | Empty-volume migration and seed; db, API, and web healthy on isolated ports | Locally verified | Temporary Compose project removed after verification; not high availability |
| Authorization | Unauthorized approval, release, and re-review returned 403; authorized flow succeeded | Locally and automatically verified | Demo identity header is not production authentication |
| Reservation lifecycle | Approve → release → re-review → re-approve → execute completed over HTTP | Locally verified | Synthetic data and simulated dispatch only |
| Transactional execution | Retry returned the existing result; consumed/execution/dispatch/registered counts were each 1 | Locally verified | Single PostgreSQL boundary, not cross-service exactly-once |
| Restart recovery | Execution status remained `registered` with a consumed reservation after container restart | Locally verified | Local container restart, not disaster recovery |
| Workflow observability | Run and node latency, model, tokens, cost, outcome displayed in the UI | Manually verified | No alerting, retention SLO, or external observability platform |
| Portfolio delivery | Static HTML returned HTTP 200 from a local server | Locally verified | Public Vercel deployment is pending |

## Continuous integration gate

The GitHub Actions workflow has three jobs. The Docker job starts only after the two quality jobs succeed:

1. **Backend · pytest**
   - Python 3.11 and uv
   - production and development requirements
   - pgvector PostgreSQL through Testcontainers
   - the complete pytest suite
2. **Frontend · lint and build**
   - Node.js 22
   - deterministic `npm ci`
   - ESLint
   - TypeScript and Vite production build
3. **Docker · stack smoke and image scan**
   - builds the API and web images and starts the Compose stack;
   - smoke-tests the web proxy and API health/observability routes; and
   - scans both images for fixed HIGH and CRITICAL vulnerabilities with the recorded ignore file.

The workflow has read-only repository permissions, a bounded timeout, dependency caching, and cancellation of superseded runs. It runs only when AssetFlow or its workflow changes.

The first successful repository run was recorded for commit `6d12b6d96abb743f9c79603113f35eb9fa48bd0f` on 2026-10-05:

- **Backend · pytest** — succeeded in 26 seconds;
- **Frontend · lint and build** — succeeded in 11 seconds; and
- **Workflow result** — succeeded in 29 seconds overall.

## Manual end-to-end evidence

The demonstrated path covered:

1. an employee submits a natural-language asset replacement request;
2. the workflow loads assignments, retrieves policy candidates, and produces a bounded proposal;
3. an operator without approval permission receives a server-side denial;
4. an authorized operator approves the versioned proposal;
5. execution revalidates ownership, approval, remaining quantity, policy version, and stock;
6. allocation and stock change in one transaction;
7. one simulated dispatch and one execution-ledger record remain; and
8. node-level latency, model usage, tokens, cost, and result are visible in observability.

Screenshots of request intake, approval management, and observability are stored under `portfolio/assets/`.

## Reservation verification on 2026-10-06

An isolated Compose project with a new volume was used so the normal local database was not changed.

1. Alembic upgraded an empty PostgreSQL database to head and the development seed completed.
2. Approval A reserved the final assignment-item capacity; competing approval B returned 409 and moved to `needs_review`.
3. Releasing A revoked only its linked approval and freed the reservation.
4. B returned through re-review, was approved with a new reservation, and executed.
5. Repeating B's execution returned the same execution key without a second mutation.
6. The status endpoint returned `registered` and reservation `consumed` before and after container restart.
7. Unauthorized approval, release, and re-review each returned 403.
8. SQL checks found zero held-stock, assignment-capacity, and consumed-dispatch invariant violations.

The branch containing this change was still ahead of its remote during verification. Therefore the older
GitHub-verified CI row above remains historical evidence only. This workflow runs for pull requests,
pushes to `main`, or manual dispatch; current-branch verification therefore requires one of those events.

## Evidence not claimed

- production authentication or identity-provider integration;
- public backend deployment or production traffic;
- high availability, autoscaling, recovery-time objectives, or disaster recovery;
- automatic reservation expiry or replacement-proposal version creation;
- CI-enforced branch protection until the repository setting is enabled;
- real employee, policy, inventory, or dispatch data;
- an independently authored large evaluation set; or
- production-generalizable accuracy.

After the Vercel deployment, add its immutable deployment URL here rather than replacing the scope boundaries above.
