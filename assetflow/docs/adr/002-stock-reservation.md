# 002. Reserve quantity when a proposal is approved

Date: 2026-10-06
Status: Accepted, implementation pending (2026-10-06). Nothing is implemented yet. This record supersedes only the inventory decision of [ADR-001](001-claim-execution.md). Until it is implemented, the code still follows ADR-001. The status changes to "Accepted, implemented and verified" only after the code and the tests listed under Invariants are complete.

Scope rule: the reservation feature is built as one complete vertical change (schema and migration, reservation on approval, approval guard, consumption on execution, release and revoke, re-review, quantity fields in the API, minimal approval screen change, tests, schema and flow documents). If it cannot be finished and verified, none of it is shipped, and the current implementation is submitted. The record then stays "Accepted, implementation pending" and is described as an unimplemented design decision, not as a feature.

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

Found by reading the code. Items marked "not run" have not been exercised.

- `graph.py` writes every proposal with `version = 1`. No code path creates version 2. Version binding exists in the schema and in the execution check, but no workflow produces a second version.
- The approval endpoint (`routers/proposals.py`) does not touch stock or the assignment item. It does not check the current state of the request and sets `request.state` unconditionally, so approving or rejecting a `registered` request would overwrite its state (not run).
- The execution transaction locks `assignment_item`, then `asset_stock`. It never locks the `request` row explicitly; the row is locked only by the `UPDATE request` near the end. Its approval lookup expects at most one approving row (`one_or_none`).
- `get_conn` rolls back the whole transaction on any exception. A refusal that raises `HTTPException` discards earlier writes in the same request, including the `audit_log` denial rows written in the approval and execution paths (not run).
- `asset_stock.available_qty` is the quantity that can be executed now. Because nothing is reserved, it also equals the quantity on hand.
- `mark_outcome_unknown()` and `resolve_outcome()` exist in `services/execution.py` and are covered by tests of the functions alone. No API path calls them.
- A request can reach `needs_review` through the graph (for example pending inspection) without having a proposal.
- The development seed creates models, stock, employees, assignments, inspections and policies. It creates no requests, proposals or approvals.

## Decision

1. **Approval reserves.** Approving a proposal reserves its quantity against two limits: the stock of the asset model and the remaining quantity of the assignment item. The approval record and the reservation are created in one transaction and commit or roll back together.
   - Reservable stock = `on_hand_qty` - sum of `held` reservations for the model.
   - Reservable item quantity = `qty - allocated_qty` - sum of `held` reservations for the item.
   - Both must be sufficient.
2. **Insufficient quantity.** If the reservable quantity is not enough, no approval is created and the request moves to `needs_review`. This is an intended business outcome and is committed. The API returns an explicit result object with HTTP status 409. It does not raise `HTTPException`, and it commits the `needs_review` transition and the audit record of the refusal in the same transaction. Committing and then raising a separate exception is rejected because the transaction boundary becomes hard to follow.
3. **Missing approval permission.** In the approval endpoint, a user without approval permission gets an explicit result object with HTTP status 403. The denial audit record is committed in the same call. It is not raised as `HTTPException`, which would roll the audit record back. The same rule applies to the new release and re-review actions. Denial audit records in the execution path are outside this record.
4. **Reservation table.** Reservations live in a separate `stock_reservation` table with states `held`, `consumed` and `released`. Each row stores the request, the proposal (`proposal_id`, a foreign key to the actual proposal), the proposal version, the payload digest, the assignment item, the asset model and the quantity. Only `held` rows reduce the reservable quantities. History stays in the table.
5. **Column meaning.** `asset_stock.available_qty` is renamed to `on_hand_qty`. Only a committed execution decreases it. Reservations never change it.
6. **Execution consumes a reservation.** Execution does not compete for free quantity. It converts the reservation that belongs to the request from `held` to `consumed`. It must check that the reservation belongs to this request, that its `proposal_id`, version and digest all equal those of the current approved proposal, that it is `held`, that its item and quantity equal the executed item and quantity, and that `on_hand_qty` is not lower than the reserved quantity. Execution must refuse when no `held` reservation exists even if an approval record exists. Its approval lookup must ignore revoked approvals (decision 8). On success, one transaction does all of: reservation `held` to `consumed`, `on_hand_qty` decrease, `allocated_qty` increase, dispatch record, execution outcome.
7. **Unknown outcome.** A lost response alone does not justify a compensating change or a re-execution. If the database can be queried, the execution key shows whether the transaction committed and what state the reservation is in: committed means `consumed`, rolled back means `held`. If the database cannot be reached, the caller treats the result as unknown and neither guesses nor changes any state. When the connection is back, the outcome is reconciled with the execution key, and only then is the request marked `outcome_unknown` if that is still needed. `mark_outcome_unknown()` and `resolve_outcome()` are not connected to any API path today. Connecting them and testing that path is part of the implementation and verification scope of this record.
8. **Release and revoke.** There is no automatic expiry. A minimum manual release action is implemented:
   - requires approval permission;
   - requires a reason;
   - releases only `held` reservations and never `consumed` ones;
   - repeating a release has no further effect;
   - writes an audit record;
   - in one transaction: sets the reservation to `released`, revokes the approval, sets the request to `needs_review`.

   Approval records are never deleted. `approval` gets `revoked_at`, `revoked_by` and `revoke_reason`. An approval is active only if `decision = 'approve'` and `revoked_at IS NULL`.
9. **Re-review.** An explicit operator action returns a request from `needs_review` to `awaiting_approval`, so the same proposal can be approved again. It requires:
   - approval permission and a recorded reason;
   - current state `needs_review`;
   - an existing proposal for the request;
   - no active approval and no `held` reservation;
   - no `consumed` reservation and no row in `request_execution` for the proposal;
   - the proposal has not been replaced.

   The state change and the audit record commit in one transaction, and repeating the call causes no second state change. Both a request that became `needs_review` because of insufficient quantity and a request released by decision 8 can return this way. Changing the content of a proposal needs a new version and is not supported here.
10. **Approval guard and indexes.** Approval is allowed only when the request is `awaiting_approval`. A proposal that already has an active approval or a live reservation cannot be approved again. The check is made after the request row is locked, so two simultaneous approvals cannot both pass it. The database enforces it too:
    - the existing `one_active_approval_per_proposal` index is replaced by a partial unique index on active approvals only (`decision = 'approve' AND revoked_at IS NULL`), at most one per proposal;
    - a partial unique index on `stock_reservation (request_id, proposal_version)` for rows in `held` or `consumed` allows at most one live reservation.
11. **Lock order.** Every transaction that touches these tables takes locks in this order, skipping the ones it does not need:
    `request` -> `assignment_item` -> `asset_stock` -> `stock_reservation`.
    - Approval locks the request row first, checks the state, then locks the item and the stock row, computes the reservable quantities, and inserts the reservation.
    - Execution is changed to lock the request row first, then the item and the stock row as it does today, then the reservation row.
    - Release and re-review lock the request row, then the reservation row.
12. **What the operator sees.** The pending-proposals response separates the quantities and the approval screen shows the reservable ones by default:
    - stock: `on_hand_qty`, `held_qty` (other active reservations), `reservable_qty`;
    - assignment item: `remaining_qty`, `held_item_qty`, `reservable_item_qty`.
13. **Scope of supersession.** Only the inventory decision of ADR-001. The server-issued execution key, PostgreSQL as the business ledger and the single transaction boundary remain in force.

### Why this lock order

The order `assignment_item -> asset_stock -> request -> stock_reservation` was considered, because execution already takes `assignment_item` and then `asset_stock`. It is rejected for two reasons.

- It makes every approval, including invalid and duplicate ones, lock the contended item and stock rows before the cheap request-state check can refuse it.
- Execution does not lock the request row today, only updates it at the end. If approval takes the request lock first and execution keeps `assignment_item -> asset_stock -> request`, the two paths order the same locks differently. A deadlock then needs one request to be in an approval and an execution at the same moment. The state guard makes that unlikely, because an approval attempt on a request that is already `ready_to_execute` is refused right after it locks the request. This is a reason the risk is low, not a proof. Relying on the guard to avoid a deadlock is fragile, so the same order is used everywhere.

The cost is a change to the execution transaction: an explicit lock on the request row at the start. Concurrent executions with the same key then also serialize on the request row instead of racing to the unique index.

## Not covered

Replacing a proposal with a new version is not part of this implementation. No code path creates a second version today. Before such a path is added, a separate record must decide when the old reservation is released and must require that release and the new reservation happen under the same locks in one transaction.

Denial audit records in the execution path are rolled back by `get_conn` as well (not run, found by reading). They are not fixed by this record and are tracked as a separate work item. Approval-path denials are covered by decisions 2 and 3.

## Existing data and tests

Existing data:

- The development seed creates no requests, proposals or approvals, so there is nothing to back-fill there.
- For any existing real database, query the approvals that can still be executed before the migration: `decision = 'approve'`, the request state is `ready_to_execute` or `outcome_unknown`, and there is no row in `request_execution` for it. If there are none, nothing is back-filled. If there are some, the migration stops and they are handled explicitly. A reservation is never guessed.
- Requests that are already `registered` are not back-filled. No reservation is created for an execution that has already happened.
- `outcome_unknown` is included because such a request can still be resolved to an executable state.
- The new `revoked_*` columns are nullable, so existing approval rows stay active.

Impact of the column rename (`available_qty` to `on_hand_qty`), found by search:

- Code: `services/execution.py` (2 places), `routers/proposals.py` (1), `services/nodes.py` (1), `seed_dev.py`, `demo.py`.
- Migration: a new migration renames the column. The original migration is not edited.
- Tests: `tests/test_execution.py` (3 places).
- Documents: `SCHEMA.md`, `SCHEMA-ERD.md`, `FLOW.md`.
- The pending-proposals response changes shape (decision 12), so the frontend type changes too.

Impact on tests, by search of the test files, to be confirmed with the full test list before implementation:

- Likely to change: `tests/test_execution.py` (14 tests, shared `replacement_case` fixture inserts the approval, proposal, request and stock), `tests/test_api.py` (2 tests, `approval_case` fixture), the approval-related tests in `tests/test_constraints.py` (2 of 8), and the `proposal_ids` fixture in `tests/conftest.py`.
- No reference to approval or stock: `test_graph.py`, `test_target_verification.py`, `test_concurrency.py`, `test_smoke.py`. Not expected to change.
- The statement "all 47 tests must change" is not supported.

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

These become binding when this record is accepted. Until then they are part of the draft.

- Per asset model, the sum of `held` reservations never exceeds `on_hand_qty`.
- Per assignment item, `allocated_qty` plus the sum of `held` reservations never exceeds `qty`.
- An approval and its reservation commit together or roll back together.
- A proposal has at most one active approval (not revoked), and a currently approved proposal has exactly one live reservation. The database refuses a second of either.
- A revoked approval cannot authorize an execution.
- A reservation records `proposal_id`, version, digest, item, model and quantity. These all match the current proposal and the execution input at execution time, and execution without a `held` reservation is refused.
- Only `held` to `consumed` and `held` to `released` are allowed, and `consumed` and `released` are final.
- A refused approval for insufficient quantity leaves reservations, stock and allocated quantity unchanged, leaves no approval row, and leaves the request in `needs_review`. The refusal audit record is kept.
- A refused approval for missing permission changes nothing except that its audit record is kept.
- Two simultaneous approvals for the last units create at most one reservation, for stock and for the same item.
- A second approval of the same request creates no second reservation and does not change the request state.
- Approving a request that is not `awaiting_approval` changes nothing.
- Release of a `consumed` reservation is refused. A second release of a `released` reservation has no further effect. A release revokes the approval, releases the reservation and sets `needs_review` together or not at all.
- Re-review is refused unless the request is `needs_review`, has a proposal, and has no active approval and no `held` reservation. Repeating it causes no second change.
- A proposal that has a `consumed` reservation or a row in `request_execution` can never be returned to `awaiting_approval`, whatever state the request is in.
- Simultaneous approval and execution of the same request do not deadlock and respect the lock order.
- A lost execution response is resolved by the execution key without a second mutation. An unreachable database leads to no state change and no guessed state.
