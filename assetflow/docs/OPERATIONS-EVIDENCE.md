# AssetFlow Operations Evidence

This document separates reproducible evidence from planned or unverified production claims. AssetFlow is a locally verified portfolio MVP, not a production deployment.

Status labels used below:

- **Local test run**: automated tests executed on the developer machine (pytest with a Testcontainer, ESLint, Vite build).
- **Local Docker check**: commands run against an isolated Compose stack on the developer machine.
- **Manual**: observed by a person in the running UI.
- **GitHub-verified**: a GitHub Actions run for the recorded commit.
- **GitHub-verified (historical)**: a GitHub Actions run for an older commit. It is not evidence for code merged later.
- **Not yet run**: no result exists.

**Remote CI status (2026-10-08): passed.** The reservation work and the read-API authorization change were merged through [PR #1](https://github.com/KatherineJH/portfolio_2026/pull/1) from `feat/stock-reservation`. On [PR #1](https://github.com/KatherineJH/portfolio_2026/pull/1) and on the `main` merge commit [`9214823`](https://github.com/KatherineJH/portfolio_2026/commit/92148238f54a887069c6ded1b5c7d5ecc7ebaccc/checks), all three jobs succeeded: Backend · pytest, Frontend · lint and build, and Docker · stack smoke and image scan. Rows marked as local checks below remain local evidence; CI does not repeat the Docker end-to-end steps.

## Evidence matrix

| Area | Evidence | Status | Scope boundary |
|---|---|---|---|
| Backend quality gate | `pytest -q` → `287 passed` on 2026-10-07 | Local test run | Uses a pgvector PostgreSQL Testcontainer; no OpenAI call; one non-blocking LangGraph warning |
| Concurrency regression | 24 concurrency tests passed three consecutive runs on 2026-10-06; on 2026-10-07 they passed once within the 287-test run | Local test run | PostgreSQL row locks and forced overlap; not a throughput benchmark |
| Frontend static analysis | `npm run lint` completed with no errors on 2026-10-07 | Local test run | ESLint only; no browser automation suite |
| Frontend production build | TypeScript and Vite production build completed on 2026-10-07 | Local test run | Build success is not a public deployment |
| CI quality gate (historical) | [`6d12b6d`](https://github.com/KatherineJH/portfolio_2026/commit/6d12b6d96abb743f9c79603113f35eb9fa48bd0f/checks) — run #2 completed in 29 seconds; backend and frontend jobs succeeded | GitHub-verified (historical) | Predates the reservation work and the Docker job; branch protection is not enabled |
| CI quality gate (current) | [PR #1](https://github.com/KatherineJH/portfolio_2026/pull/1) and `main` merge commit [`9214823`](https://github.com/KatherineJH/portfolio_2026/commit/92148238f54a887069c6ded1b5c7d5ecc7ebaccc/checks) — Backend · pytest, Frontend · lint and build, and Docker · stack smoke and image scan all succeeded on 2026-10-08 | GitHub-verified | Evidence is tied to the tested commits; branch protection is not enabled |
| Full-stack runtime | Empty-volume migration and seed; db, API, and web healthy on isolated ports (2026-10-06, repeated 2026-10-07) | Local Docker check | Temporary Compose project removed after verification; not high availability |
| Authorization | pytest covers 403 for approval, release, and re-review and 422/401/403/200 for every read API; the Docker check confirmed the same codes over HTTP (2026-10-06 and 2026-10-07) | Local test run and local Docker check | Demo identity header is not production authentication |
| Reservation lifecycle | Approve → release → re-review → re-approve → execute completed over HTTP (2026-10-06) | Local Docker check | Synthetic data and simulated dispatch only |
| Transactional execution | Retry returned the existing result; consumed/execution/dispatch/registered counts were each 1 (2026-10-06) | Local Docker check | Single PostgreSQL boundary, not cross-service exactly-once |
| Restart recovery | Execution status remained `registered` with a consumed reservation after container restart (2026-10-06) | Local Docker check | Local container restart, not disaster recovery |
| Workflow observability | Run and node latency, model, tokens, cost, outcome displayed in the UI | Manual | No alerting, retention SLO, or external observability platform |
| Portfolio delivery | Static HTML returned HTTP 200 from a local server | Manual | Public Vercel deployment is pending |

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
   - smoke-tests the web proxy, API health, and the authenticated observability route (an unregistered user must receive 401); and
   - scans both images for fixed HIGH and CRITICAL vulnerabilities with the recorded ignore file.

The workflow has read-only repository permissions, a bounded timeout, dependency caching, and cancellation of superseded runs. It runs only when AssetFlow or its workflow changes.

On 2026-10-08, [PR #1](https://github.com/KatherineJH/portfolio_2026/pull/1) and the `main` merge commit [`9214823`](https://github.com/KatherineJH/portfolio_2026/commit/92148238f54a887069c6ded1b5c7d5ecc7ebaccc/checks) passed all three jobs, including the Docker job and its read-API 401 smoke check.

The first successful repository run, which predates the Docker job, was recorded for commit `6d12b6d96abb743f9c79603113f35eb9fa48bd0f` on 2026-10-05:

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

All steps above are a local Docker check. GitHub Actions does not run these end-to-end steps; the 2026-10-08 CI run covers the test suite, frontend checks, and the Docker smoke and image scan.

## Read-API authorization verification on 2026-10-07

Local test run:

- `pytest -q` → `287 passed`. The 25 new tests are 4 in `test_pending_quantities.py` and 21 in `test_read_authorization.py`.
- With the read-API authorization checks removed, the 12 refusal cases in `test_read_authorization.py` failed, so the tests detect a missing check.
- `npm run lint` and `npm run build` succeeded.

Local Docker check (isolated Compose project, empty volume, removed afterwards):

- the updated CI smoke step passed: no identity → 422, unregistered user → 401 on `/api/observability/runs`;
- after the development seed: operators with and without approval rights → 200 on runs; an operator → 200 on pending proposals; an employee → 403 on runs, pending proposals, and request trace; an employee reading their own assignments → 200, another employee → 403, an operator → 200.

Remote CI (2026-10-08): [PR #1](https://github.com/KatherineJH/portfolio_2026/pull/1) and the `main` merge commit [`9214823`](https://github.com/KatherineJH/portfolio_2026/commit/92148238f54a887069c6ded1b5c7d5ecc7ebaccc/checks) passed Backend · pytest, Frontend · lint and build, and Docker · stack smoke and image scan. The Docker job ran the updated smoke step that expects 401 for an unregistered user.

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
