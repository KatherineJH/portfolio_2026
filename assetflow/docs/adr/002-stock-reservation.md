# 002. Reserve quantity when a proposal is approved

Date: 2026-10-06
Status: Accepted, implemented and locally verified (2026-10-06). This record supersedes only the inventory decision of [ADR-001](001-claim-execution.md). The server-issued execution key, PostgreSQL authority, and single application/database transaction boundary from ADR-001 remain in force.

Implementation note: the vertical change is complete in the feature branch: schema and migration, reservation on approval, approval guard, consumption on execution, release and revoke, re-review, outcome lookup, quantity fields and operator UI, automated tests, Docker E2E, and documentation. Current-branch CI remains unverified until the branch is pushed and GitHub Actions succeeds.

## Context

ADR-001 chose not to reserve anything while a proposal waits for approval. Stock and the remaining quantity of the assignment item are checked and allocated only inside the execution transaction, so an approval does not guarantee that the request can be executed.

Example under that rule:

- Stock for USB-C Dock is 5.
- Request A (qty 2) is approved. Stock is still 5.
- Other approved requests execute first and stock drops to 1.
- Executing request A is rejected with "available stock 1, requested 2". Nothing changes and stock does not go negative.

The same can happen with the assignment item. Two approved requests can target the same item. If its remaining quantity (`qty - allocated_qty`) is smaller than both requests together, the later one fails at execution even when stock is sufficient.

The data stays consistent, but an operator who approved a request sees an approved request that cannot be executed. The owner decided that approval should mean the quantity is set aside for that request. The change is made because of what approval means in the business flow, not because of an external analogy.

### Verified facts about the current code (2026-10-06)

Pre-implementation facts recorded when this ADR was drafted. They explain the change and do not describe the current code. Items marked "not run" were code-reading findings at that time.

- `graph.py` writes every proposal with `version = 1`. No code path creates version 2. Version binding exists in the schema and in the execution check, but no workflow produces a second version.
- The approval endpoint (`routers/proposals.py`) does not touch stock or the assignment item. It does not check the current state of the request and sets `request.state` unconditionally, so approving or rejecting a `registered` request would overwrite its state (not run).
- The execution transaction locks `assignment_item`, then `asset_stock`. It never locks the `request` row explicitly; the row is locked only by the `UPDATE request` near the end. Its approval lookup expects at most one approving row (`one_or_none`).
- `get_conn` rolls back the whole transaction on any exception. A refusal that raises `HTTPException` discards earlier writes in the same request, including the `audit_log` denial rows written in the approval and execution paths (not run).
- Before this ADR, `asset_stock.available_qty` represented both executable quantity and quantity on hand. The migration renamed it to `on_hand_qty`; reservable quantity is now computed by subtracting `held` reservations.
- `resolve_outcome()` exists in `services/execution.py`; step 6 exposes it through an operator-only status endpoint. The earlier `mark_outcome_unknown()` helper was removed because an unavailable database cannot persist that state.
- A request can reach `needs_review` through the graph (for example pending inspection) without having a proposal.
- The development seed creates models, stock, employees, assignments, inspections and policies. It creates no requests, proposals or approvals.

## Decision

1. **Approval reserves.** Approving a proposal reserves its quantity against two limits: the stock of the asset model and the remaining quantity of the assignment item. The approval record and the reservation are created in one transaction and commit or roll back together.
   - Reservable stock = `on_hand_qty` - sum of `held` reservations for the model.
   - Reservable item quantity = `qty - allocated_qty` - sum of `held` reservations for the item.
   - Both must be sufficient.
2. **Insufficient quantity.** If the reservable quantity is not enough, no approval is created and the request moves to `needs_review`. This is an intended business outcome and is committed. The API returns an explicit result object with HTTP status 409. It does not raise `HTTPException`, and it commits the `needs_review` transition and the audit record of the refusal in the same transaction. Committing and then raising a separate exception is rejected because the transaction boundary becomes hard to follow.
3. **Missing approval permission.** In the approval endpoint, a user without approval permission gets an explicit result object with HTTP status 403. The denial audit record is committed in the same call. It is not raised as `HTTPException`, which would roll the audit record back. The same rule applies to the new release and re-review actions. Denial audit records in the execution path are outside this record.
4. **Audited refusals and pre-approval checks.** Every refusal made by a business rule writes a `denied` audit record with a `reason_code` and a short detail, and the record is committed. The reason codes are `no_permission`, `not_awaiting_approval`, `already_approved`, `already_reserved`, `invalid_proposal`, `item_not_owned_by_requester`, `insufficient_item_quantity` and `insufficient_stock`. Plain input errors are not audited: an invalid `decision` value, an unknown proposal id, and a body whose `proposal_id` differs from the path (HTTP 400 or 404). Before anything is reserved, the approval checks that the proposal is a replacement (both the `action_type` column and the payload `action` say `replacement`), that the payload names an existing assignment item and a positive integer quantity, and that the item belongs to the employee who made the request. Without the ownership check, a proposal for someone else's item could be approved and reserve stock, and would fail only at execution. When both limits are short, the assignment item limit is reported first.
5. **Reservation table.** Reservations live in a separate `stock_reservation` table with states `held`, `consumed` and `released`. Only `held` rows reduce the reservable quantities. History stays in the table. The database, not only the application, keeps each row consistent with the records it points to:
   - `approval_id` is `NOT NULL`, `UNIQUE` and references the approval that created the reservation. The reservation's request, proposal, proposal version and payload digest must equal those of that approval (composite foreign key). The approval must be an `approve` decision (a constant `approval_decision` column checked to be `'approve'` and included in the composite key). A reservation is created with the id returned by the approval insert in the same transaction, so a failed reservation rolls the approval back.
   - `(proposal_id, request_id, proposal_version)` must match a real proposal (composite foreign key to a new `UNIQUE (id, request_id, version)` on `proposal`).
   - `(assignment_item_id, asset_model_id)` must match a real assignment item (composite foreign key to a new `UNIQUE (id, asset_model_id)` on `assignment_item`), and `asset_model_id` must have a stock row.
   - `execution_key` is nullable, `UNIQUE`, and references `request_execution` together with `request_id` and `proposal_version` (new `UNIQUE (execution_key, request_id, proposal_version)` on `request_execution`).
   - One `CHECK` allows exactly three shapes: `held` with `resolved_at IS NULL` and `execution_key IS NULL`; `released` with `resolved_at IS NOT NULL` and `execution_key IS NULL`; `consumed` with `resolved_at IS NOT NULL` and `execution_key IS NOT NULL`.
   - `qty > 0`.

   What the database cannot enforce stays with the application and its tests: that a reservation's item and quantity equal the proposal payload, and that a reservation is released when its approval is revoked.
6. **Column meaning.** `asset_stock.available_qty` is renamed to `on_hand_qty`, and its check constraint `stock_not_negative` is renamed to `on_hand_qty_not_negative`. The downgrade restores both original names. Only a committed execution decreases `on_hand_qty`. Reservations never change it.
7. **Execution consumes a reservation.** Execution does not compete for free quantity. It converts the reservation that belongs to the request from `held` to `consumed`. It must check that the reservation belongs to this request, that its `proposal_id`, version and digest all equal those of the current approved proposal, that it is `held`, that its item and quantity equal the executed item and quantity, and that `on_hand_qty` is not lower than the reserved quantity. Execution must refuse when no `held` reservation exists even if an approval record exists. It also refuses a proposal whose `action_type` is not `replacement`, and a request whose state is not `ready_to_execute` or `outcome_unknown`. The request row is locked first. A retry with an execution key that already exists is answered before the state check, because a finished request is `registered`. The reservation is read once without a lock, so a bad request is refused before the contended item and stock rows are locked. It is then locked after the item and stock rows and read again, and consumed with an update conditioned on `status = 'held'` that must change exactly one row. Its approval lookup must ignore revoked approvals (decision 9). On success, one transaction does all of: reservation `held` to `consumed`, `on_hand_qty` decrease, `allocated_qty` increase, dispatch record, execution outcome.
8. **Unknown outcome.** A lost response alone does not justify a compensating change or a re-execution. If the database can be queried, the execution key shows whether the transaction committed and what state the reservation is in: committed means `consumed`, rolled back means `held`. If the database cannot be reached, the caller treats the result as unknown and neither guesses nor changes database state. All `get_conn` dependencies use FastAPI function scope, supported by the pinned FastAPI 0.142 release, so commit completes before an HTTP success response is sent. A connection failure returns HTTP 503; mutating methods additionally return the temporary response field `outcome: unknown`. SQL and constraint errors are not mislabeled as connection failures. After connectivity returns, an IT operator resolves the result through `GET /executions/status?request_id=&proposal_version=`, which reconstructs the server execution key and returns the ledger outcome plus request and reservation states. No new request is written as `outcome_unknown`; the enum value and executable-state compatibility remain only for existing databases.
9. **Release and revoke.** There is no automatic expiry. A minimum manual release action is implemented:
   - requires approval permission;
   - requires a reason;
   - releases only `held` reservations and never `consumed` ones;
   - repeating a release has no further effect;
   - writes an audit record;
   - in one transaction: sets the reservation to `released`, revokes the approval, sets the request to `needs_review`.

   Details fixed in step 4. The endpoint is `POST /proposals/{proposal_id}/release` with a body `{"reason": ...}`. The reservation to release is the latest reservation of the given proposal, ordered by `created_at DESC, id DESC`; a reservation of another proposal of the same request is never touched. Only the approval linked to that reservation by `approval_id` is revoked; the other approvals and rejections of the same proposal, including approvals that were already revoked, keep their data. A missing or blank reason is a plain input error (HTTP 400) and is not audited. A release succeeds only while the request is `ready_to_execute`; an `outcome_unknown` request must be resolved first (decision 8). A second release of an already released reservation answers `already_released` with no change and no audit record, and a consumed reservation is refused with `already_consumed`. A successful release writes an `allowed` audit record with the reason; refusals write `denied` records with their reason code (`no_permission`, `no_reservation`, `already_consumed`, `not_ready_to_execute`). Locks are taken in the same order as execution and approval, request row first and then the reservation row, so a release racing an execution of the same request cannot deadlock. The reservation is read once, then locked and read again, and it is released by an update conditioned on `status = 'held'` that must change exactly one row; the approval revocation must also change exactly one row.

   Approval records are never deleted. `approval` gets `revoked_at`, `revoked_by` and `revoke_reason`. An approval is active only if `decision = 'approve'` and `revoked_at IS NULL`.
10. **Re-review.** An explicit operator action returns a request from `needs_review` to `awaiting_approval`, so the same proposal can be approved again. It requires:
   - approval permission and a recorded reason;
   - current state `needs_review`;
   - an existing proposal for the request;
   - no active approval and no `held` reservation;
   - no `consumed` reservation and no row in `request_execution` for the proposal;
   - the proposal has not been replaced.

   The state change and the audit record commit in one transaction, and repeating the call causes no second state change. Both a request that became `needs_review` because of insufficient quantity and a request released by decision 9 can return this way. Changing the content of a proposal needs a new version and is not supported here.

   Details fixed in step 5. The endpoint is `POST /proposals/{proposal_id}/re-review` with a body `{"reason": ...}`. A request that the graph closed as `needs_review` without a proposal has nothing to call it with, so it is out of scope by construction. After the request row is locked, the checks run in this order, and the order is the rule: (1) the proposal was executed, meaning a `request_execution` row for the request and version or a `consumed` reservation for the proposal, answers `already_executed` whatever the request state is, including `awaiting_approval` and `registered`; (2) a newer proposal version exists, `proposal_replaced`, even if the request is already `awaiting_approval`; (3) only for a proposal that passed both, a request already `awaiting_approval` answers `already_awaiting_approval` with no change and no audit record, and any other state except `needs_review` is `not_in_review`; (4) an active approval, `approval_still_active`; (5) a `held` reservation, `reservation_still_held`. The last two are inconsistent data; the held reservation lookup locks the rows, request first and then reservation. The state change runs in a savepoint together with the `allowed` audit record, so a failing audit insert undoes the state change, and it updates only a row still in `needs_review` and must change exactly one row. Refusals write `denied` records with their reason code. A missing or blank reason is a plain 400 and is not audited. A legacy execution made before reservations existed has no `consumed` reservation, so the `request_execution` row is what makes check (1) work for it; the `consumed` part of the check is redundant with it, because a `consumed` reservation cannot exist without its execution row.
11. **Approval guard and indexes.** Approval is allowed only when the request is `awaiting_approval`. A request that already has an active approval or a live reservation (`held` or `consumed`) can be neither approved nor rejected again; a live reservation without an active approval is inconsistent data, and approving would hit the unique index while rejecting would leave the reservation orphaned. The check is made after the request row is locked, so two simultaneous approvals cannot both pass it. The database enforces it too:
    - the existing `one_active_approval_per_proposal` index is replaced by a partial unique index on active approvals only (`decision = 'approve' AND revoked_at IS NULL`), at most one per proposal;
    - a second partial unique index `one_active_approval_per_request` on `approval (request_id)` with the same predicate allows at most one active approval per request, even across different proposals. It makes the per-proposal index redundant in effect, and both are kept so each rule is stated explicitly;
    - a partial unique index `one_live_reservation_per_request` on `stock_reservation (request_id)` for rows in `held` or `consumed` allows at most one live reservation per request. A key on `(request_id, proposal_version)` would let two versions of one request each hold a reservation. A future proposal replacement must release the old reservation in the same transaction before creating the new one, which is compatible with this index.
12. **Lock order.** Every transaction that touches these tables takes locks in this order, skipping the ones it does not need:
    `request` -> `assignment_item` -> `asset_stock` -> `stock_reservation`.
    - Approval locks the request row first, checks the state, then locks the item and the stock row, computes the reservable quantities, and inserts the reservation.
    - Execution is changed to lock the request row first, then the item and the stock row as it does today, then the reservation row.
    - Release and re-review lock the request row, then the reservation row.
13. **What the operator sees.** The pending-proposals response separates the quantities and the approval screen shows the reservable ones by default:
    - stock: `on_hand_qty`, `held_stock_qty` (all active held reservations), `reservable_stock_qty`;
    - assignment item: `remaining_item_qty`, `held_item_qty`, `reservable_item_qty`;
    - the proposal's latest reservation: `reservation_status`, `reservation_qty`.
14. **Scope of supersession.** Only the inventory decision of ADR-001. The server-issued execution key, PostgreSQL as the business ledger and the single transaction boundary remain in force.

### Why this lock order

The order `assignment_item -> asset_stock -> request -> stock_reservation` was considered, because execution already takes `assignment_item` and then `asset_stock`. It is rejected for two reasons.

- It makes every approval, including invalid and duplicate ones, lock the contended item and stock rows before the cheap request-state check can refuse it.
- Execution does not lock the request row today, only updates it at the end. If approval takes the request lock first and execution keeps `assignment_item -> asset_stock -> request`, the two paths order the same locks differently. A deadlock then needs one request to be in an approval and an execution at the same moment. The state guard makes that unlikely, because an approval attempt on a request that is already `ready_to_execute` is refused right after it locks the request. This is a reason the risk is low, not a proof. Relying on the guard to avoid a deadlock is fragile, so the same order is used everywhere.

The cost is a change to the execution transaction: an explicit lock on the request row at the start. Concurrent executions with the same key then also serialize on the request row instead of racing to the unique index.

## Not covered

Replacing a proposal with a new version is not part of this implementation. No code path creates a second version today. Before such a path is added, a separate record must decide when the old reservation is released and must require that release and the new reservation happen under the same locks in one transaction.

Denial audit records in the execution path are rolled back by `get_conn` as well (not run, found by reading). They are not fixed by this record and are tracked as a separate work item. Approval-path denials are covered by decisions 2, 3 and 4.

## Existing data and tests

Existing data:

- The development seed creates no requests, proposals or approvals, so there is nothing to back-fill there.
- For any existing real database, query the approvals that can still be executed before the migration: `decision = 'approve'`, the request state is `ready_to_execute` or `outcome_unknown`, and there is no row in `request_execution` for it. If there are none, nothing is back-filled. If there are some, the migration stops and they are handled explicitly. A reservation is never guessed.
- The same pre-check also stops the migration if any request has more than one `approve` row, because the new per-request index could not be created. The pre-check runs inside the migration transaction, so a failure leaves the schema and `alembic_version` unchanged.
- The downgrade is supported only while no reservation and no revoked approval exists. It refuses otherwise and does not claim to restore data.
- Requests that are already `registered` are not back-filled. No reservation is created for an execution that has already happened.
- `outcome_unknown` is included because such a request can still be resolved to an executable state.
- The new `revoked_*` columns are nullable, so existing approval rows stay active.

Implemented impact:

- Migration `19e6a6ac7866` performs the pre-check, column and constraint rename, revocation fields, reservation table, foreign keys, and partial indexes atomically.
- Application and seed queries now use `on_hand_qty`; pending-proposal responses expose the separated reservation quantities from decision 13.
- Approval, execution, release, re-review, transaction-boundary, schema, and concurrency tests cover the new paths. The full suite contains 262 tests; the 24 concurrency tests passed three consecutive runs on 2026-10-06.
- `SCHEMA.md`, `SCHEMA-ERD.md`, `FLOW.md`, `TRACEABILITY.md`, README, and operations evidence were reconciled after implementation.

## Alternatives considered

| Alternative | Reason not chosen |
|---|---|
| Keep ADR-001 (approval is only a policy permission) | Concurrent approvals can produce approvals that cannot be executed, which does not match what an operator expects from approving. |
| Reserve when the proposal is created | Unapproved requests would hold quantity. |
| Reserve stock only, not the assignment item | The item's remaining quantity can still make an approved request fail at execution. |
| Counter column such as `reserved_qty` instead of a table | A counter can drift from the real state and keeps no history of consumed or released reservations. |
| Status check only, without row lock and unique index | Two simultaneous approvals can both pass the check. |
| Delete the approval on release | Approval history is lost. Revocation columns keep it. |
| Keep `one_active_approval_per_proposal` unchanged | A revoked approval would block approving the same proposal again. |

## Consequences

- An approval is a reliable promise that the quantity is set aside, and a lost race is reported at approval time instead of execution time.
- A held reservation blocks stock and item quantity until it is consumed or released. Without expiry, an approved request that is never executed holds quantity until someone releases it. The manual release action is the only way out.
- Three operator actions change quantities or state: approval with reservation, release, and re-review. Execution becomes a conversion of a reservation and its transaction gains one more lock.
- No cross-service exactly-once claim is made. The scope stays within one FastAPI application and one PostgreSQL transaction boundary.

## Invariants

These invariants are binding for the implemented reservation workflow.

- Per asset model, the sum of `held` reservations never exceeds `on_hand_qty`.
- Per assignment item, `allocated_qty` plus the sum of `held` reservations never exceeds `qty`.
- An approval and its reservation commit together or roll back together.
- A request has at most one active approval (not revoked) and at most one live reservation (`held` or `consumed`), even across different proposals. The database refuses a second of either.
- A reservation points to exactly one approval, which must be an `approve` decision with the same request, proposal, proposal version and digest. A consumed reservation points to a real execution of the same request and proposal version, and an execution key belongs to at most one reservation.
- A reservation's status, `resolved_at` and `execution_key` take only the three allowed combinations.
- A revoked approval cannot authorize an execution.
- A reservation records `proposal_id`, version, digest, item, model and quantity. These all match the current proposal and the execution input at execution time, and execution without a `held` reservation is refused.
- Only `held` to `consumed` and `held` to `released` are allowed, and `consumed` and `released` are final.
- A refused approval for insufficient quantity leaves reservations, stock and allocated quantity unchanged, leaves no approval row, and leaves the request in `needs_review`. The refusal audit record is kept.
- A refusal by a business rule (missing permission, wrong request state, active approval already present, malformed proposal, item of another employee, insufficient quantity) changes no business state and leaves a committed `denied` audit record with its reason code. Plain input errors (HTTP 400 and 404) leave neither.
- Two simultaneous approvals for the last units create at most one reservation, for stock and for the same item.
- A second approval of the same request creates no second reservation and does not change the request state.
- Approving or rejecting a request that is not `awaiting_approval` changes no business state and is audited.
- Release of a `consumed` reservation is refused. A second release of a `released` reservation has no further effect. A release revokes the approval, releases the reservation and sets `needs_review` together or not at all.
- Re-review is refused unless the request is `needs_review`, has a proposal, and has no active approval and no `held` reservation. Repeating it causes no second change.
- A proposal that has a `consumed` reservation or a row in `request_execution` can never be returned to `awaiting_approval`, whatever state the request is in.
- Simultaneous approval and execution of the same request do not deadlock and respect the lock order.
- A lost execution response is resolved by the execution key without a second mutation. An unreachable database leads to no state change and no guessed state.
