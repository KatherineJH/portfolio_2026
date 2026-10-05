# AssetFlow Operations Evidence

This document separates reproducible evidence from planned or unverified production claims. AssetFlow is a locally verified portfolio MVP, not a production deployment.

## Evidence matrix

| Area | Evidence | Status | Scope boundary |
|---|---|---|---|
| Backend quality gate | `uv run pytest -q` → `47 passed, 0 failed` on 2026-10-05 | Locally verified | Uses a pgvector PostgreSQL Testcontainer; no OpenAI call |
| Frontend static analysis | `npm run lint` completed with no errors on 2026-10-05 | Locally verified | ESLint only; no browser E2E suite |
| Frontend production build | TypeScript and Vite production build completed on 2026-10-04 | Locally verified | Build success is not a deployment health check |
| CI definition | `.github/workflows/assetflow-quality.yml` | Configured | A successful GitHub Actions run must be linked after the workflow is pushed |
| Database runtime | `assetflow-db` reported healthy on `127.0.0.1:5433` on 2026-10-05 | Locally observed | Single local container, not high availability |
| API runtime | `/health` returned `status: ok` during the manual demonstration | Manually verified | Local FastAPI process on port 8005 |
| Authorization | Viewer approval was rejected; authorized operator approval succeeded | Manually and partially automatically verified | Demo identity header is not production authentication |
| Transactional execution | Allocation `0 → 2`, inventory `4 → 2`, one execution ledger row, and one simulated dispatch | Manually verified | Synthetic data and simulated dispatch only |
| Workflow observability | Run and node latency, model, tokens, cost, outcome displayed in the UI | Manually verified | No alerting, retention SLO, or external observability platform |
| Portfolio delivery | Static HTML returned HTTP 200 from a local server | Locally verified | Public Vercel deployment is pending |

## Continuous integration gate

The GitHub Actions workflow has two independent jobs:

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

The workflow has read-only repository permissions, a bounded timeout, dependency caching, and cancellation of superseded runs. It runs only when AssetFlow or its workflow changes.

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

## Evidence not claimed

- production authentication or identity-provider integration;
- public backend deployment or production traffic;
- high availability, autoscaling, recovery-time objectives, or disaster recovery;
- CI-enforced branch protection until the repository setting is enabled;
- real employee, policy, inventory, or dispatch data;
- an independently authored large evaluation set; or
- production-generalizable accuracy.

After the first successful CI run and Vercel deployment, add their immutable URLs here rather than replacing the scope boundaries above.
