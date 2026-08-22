# Sandbox API

Everything Track B needs to code against after the hour-12 merge. Response
shapes come from `contracts/models.py` and do not change; request shapes are
the sandbox's own and are given in full here.

The merge is a base-URL swap:

```python
SANDBOX = StubSandbox()                              # before
SANDBOX = HttpSandbox("http://localhost:8000")       # after
```

Both satisfy the same `SandboxClient` protocol and pass the same
`tests/contract/` suite. `HttpSandbox` refuses a non-loopback base URL.

Run it:

```bash
uvicorn sandbox.app:app --port 8000
```

---

## Reads

### `GET /inventory` · `GET /inventory/{component_id}`
`list[Component]` / `Component`. 404 on an unknown id.

**Reason from `usable_stock`, never `current_stock`.** The gap between them is
scenario B-1/H-02 and it is the most common way to fail this problem.

### `GET /purchase-orders` · `GET /purchase-orders/{po_id}`
`list[PurchaseOrder]` / `PurchaseOrder`. 404 on an unknown id.

### `GET /suppliers?component_id=COMP-104`
`list[SupplierView]` — every `Supplier` field, **plus `effective_reliability`**
from the trust ledger, alongside the catalog's untouched `reliability_score`.

```json
{"supplier_id": "SUP-21", "reliability_score": 0.72,
 "effective_reliability": 0.57, "...": "all other Supplier fields"}
```

`component_id` is required; omitting it is a 422. An unknown component returns
`[]`, not an error.

`HttpSandbox.get_suppliers()` parses these into plain `Supplier` objects, so
the protocol and stub parity hold. Use `get_suppliers_with_trust()` for the
dicts including `effective_reliability`.

### `GET /production-schedule`
`list[ProductionOrder]`, ordered by deadline.

### `GET /tracking/{po_id}`
`TrackingRecord` — ground truth against supplier claims. 404 if no record.

**Side effect, by design:** a discrepancy between the claim and the evidence
records an **incident**, always — whether or not anyone is at fault — and
drops that PO's shipment confidence. Idempotent per (po_id, observed), so
polling compounds nothing and a read cache changes cost, not answers.

Reputation moves **only** when attribution comes back `SUPPLIER`.

### `GET /inbox?since=<iso8601>`
`list[Message]`, oldest first. `since` filters on `ts` strictly greater than.

A queued persona reply is invisible until its `visible_at` tick, so a
follow-up and its answer never arrive in the same read.

---

## Writes

### `POST /suppliers/{supplier_id}/message`
```json
{"subject": "PO-7712 status", "body": "Confirm the date and quantity?",
 "trust_event": null}
```
Returns the sent `Message`. 404 on an unknown supplier.

`trust_event` is optional and is one of `on_time`, `late`,
`contradicted_claim`, `moq_failure`, `quality_miss`. Supplying it records that
observation against the supplier. **The sandbox does not infer trust events
from message traffic** — the ledger is the agent's memory, and a world that
writes it behind the agent's back produces an audit trail nobody can explain.
The one exception is the delay case: if the queued reply announces a delay, a
`late` event is recorded once for that reply.

Advances the simulated clock by one hour and queues the persona's reply.

**Personas** (assigned at seed time, on `suppliers.persona`):

| persona | suppliers | behaviour |
|---|---|---|
| `honest` | SUP-37, SUP-42 | a specific date and quantity, first reply |
| `vague` | SUP-55 | no date, no quantity — until challenged |
| `contradictory` | SUP-21 | claims dispatch that its own tracking does not support; revises down when challenged |

A challenge counts when the body contains a question mark **and** the word
`date` or `quantity` on a word boundary. `"Any update?"` does not count —
"update" contains "date" as a substring and matching it would let the vaguest
possible nudge unlock the specifics.

### `POST /rfq`
```json
{"component_id": "COMP-104", "quantity": 700, "needed_by_days": 4,
 "supplier_ids": ["SUP-42", "SUP-37"]}
```
`list[Quote]`. Suppliers not carrying the component are skipped silently.
Quotes are persisted, so `quote_valid_hours` expires against the simulated
clock and G8 has something real to invalidate.

### `POST /approval/check`
```json
{"action": "create_alternate_po", "estimated_cost": 152010.0}
```
`ApprovalResult`. PS §5.8 exactly:

```
approval_required = estimated_cost > 150000
approval_reason   = "Cost exceeds autonomous purchase threshold of 150000"
                    if approval_required else None
```

Strictly greater-than. A plan costing exactly 150,000 executes autonomously.

### `POST /erp/update`
```json
{"action": "mark_po_delayed", "payload": {"po_id": "PO-7712"}}
```
→ `{"status": "ok", "message": "...", "record_id": "ERP-0001"}`

**Only these six actions** (PS §5.9). Anything else is a 400, including
correct actions in the wrong case:

`mark_po_delayed` · `create_alternate_po` · `attach_supplier_note` ·
`update_production_risk` · `record_escalation` · `store_recovery_plan`

`HttpSandbox.erp_update` converts the 400 into
`{"status": "rejected", "record_id": None}` so it matches `StubSandbox`.

Every accepted write appends to `erp_log` with its full payload and a
timestamp.

---

## Simulation control

### `GET /sim/clock` → `{"now": "2026-09-02T10:00:00"}`

The clock starts at `SIM_EPOCH` = 2026-09-02T10:00 and moves for exactly
three reasons: sending a supplier a message (+1 tick), **issuing an RFQ
(+1 tick)**, and a sequence step's `delay_minutes`. Seeded deadlines are
relative to the epoch, so PROD-914 is always day 2 and PROD-882 always day 4.

`StubSandbox` ticks identically, and a test asserts the two agree step for
step. This matters for G8: quotes are stamped from the clock and expire
against it, so a stub with a frozen clock stamps every re-quote with the same
past instant. Once the agent's clock passes the validity window, each
re-quote is born expired, G8 fires again, and the re-RFQ recovery path can
never close — terminating safely at the replan cap, which is what makes it
look like a loop rather than a fault.

### `POST /sim/reset` → reseeds from scratch, clearing the trust ledger.

### `POST /sim/inject`
```json
{"event": "H-07", "params": {}}
```
→ `{"disruption_id": "DIS-001", "event": "H-07", "ts": "...", "detail": {...}}`

`detail` always includes a `was` key with the prior value. Unknown event → 400.

| event | mutation | params (all optional) |
|---|---|---|
| H-01 | a PO's `expected_delivery` +5 days, status `delayed`, queues a message | `component_id`, `days` |
| H-02 | `current_stock` → 800, `usable_stock` → 390 | `component_id`, `current_stock`, `usable_stock` |
| H-03 | cheapest **still-eligible** supplier's quality → below `min_quality` | `component_id`, `supplier_id` |
| H-04 | SUP-37 `available_quantity` → 200 | `supplier_id`, `available_quantity` |
| H-05 | SUP-18 `lead_time_days` → 2, `reliability_score` → 0.5 | `supplier_id`, `lead_time_days`, `reliability_score` |
| H-06 | `daily_usage` → 130 | `component_id`, `daily_usage` |
| H-07 | expedite off for all future quotes for the component | `component_id` |
| H-08 | tracking → contradicting, queues a "dispatched" message | `po_id` |
| H-09 | every alternate's `unit_price` ×1.4, incumbent untouched | `component_id`, `factor` |
| H-10 | PROD-914 → `high`, `max_delay_days` → 0 | `production_order_id`, `priority`, `max_delay_days` |

H-03 deviates from §4.8, which names SUP-18: SUP-18 is already uncertified
*and* already below the floor, so mutating it injects nothing observable.

**Read `demo/HIDDEN_TEST_RESULTS.md` before relying on any of these.** Seven
of the ten do not move the plan, for reasons that are structural rather than
accidental.

### `POST /sim/inject/sequence`
```json
{"steps": [{"event": "H-06", "params": {}, "delay_minutes": 0},
           {"event": "H-07", "params": {}, "delay_minutes": 30}]}
```
Applied in order, advancing the clock by each step's delay. Returns a list of
inject results.

---

## Incidents and attribution

An incident is an observation about a shipment. Attribution is a separate
judgement recorded alongside it, never a precondition for recording it.

| attribution | established by |
|---|---|
| `SUPPLIER` | nothing was ever tendered **and** the supplier's own record shows no pack event |
| `COURIER` | handover scanned, then no movement for 48h |
| `UNATTRIBUTED` | the evidence does not establish a cause |
| `FACTORY`, `EXTERNAL` | in the vocabulary for incidents arriving from outside tracking; no tracking record returns them |

**A label with no pickup scan is `UNATTRIBUTED`.** It fits a supplier that
never handed the goods over and equally a courier that never came. Nothing in
tracking separates them, so no reputation moves — the shipment is still
unverifiable, which is a different statement about a different thing.

`trust.incidents_for(po_id=..., supplier_id=...)` returns the log for the
audit trail and the brief. Each row carries `observed`, `expected`,
`attribution` and `attribution_basis` — the basis being one readable line
giving the reason.

## The two trust axes

```python
from trust import reputation, shipment_confidence, units_confirmed
from trust import effective_reliability          # signature unchanged
```

| | `reputation(supplier_id)` | `shipment_confidence(po_id)` |
|---|---|---|
| about | a counterparty | one consignment |
| moves on | `SUPPLIER`-attributed incidents only | any verified discrepancy |
| speed | slow, historical, sticky | fast, per-PO |
| feeds | `effective_reliability` → the solver risk term | whether in-transit units count as confirmed |

`effective_reliability(supplier_id, catalog_score)` is unchanged in signature
and in meaning: catalog score minus attributed history, floored at 0.05.

`units_confirmed(po_id)` is the boolean form — false below 0.5.
`solver.build.unconfirmed_shipment_suppliers(client)` gives the set of
suppliers whose units cannot be counted, which is what to pass as
`build_solver_input(contradicted=...)`. That argument keeps its name because
`SolverSupplier.claim_contradicted` is frozen; read it as *unconfirmed*.

---

## `validate(plan, context) -> Verdict`

`from guardrails.validator import validate`

Six post-checks. G3, G4, G6, G7 and G11 are pre-solve filters or model
constraints and never appear in a `Verdict`; if one does, the solver built a
plan it should not have been able to construct.

### Context keys

Every key except `approval_limit` is optional. **A rule whose inputs are
absent does not fire** — calling `validate` before projected stock is known
gets you no G5, not a crash.

| key | type | read by |
|---|---|---|
| `approval_limit` | float | G2 — **frozen name** |
| `remaining_budget` | float | G1 |
| `projected_stock` | int | G5 |
| `safety_stock` | int | G5 |
| `affected_priorities` | `list[str]` | G5 |
| `safety_stock_breach_justification` | `str \| None` | G5 |
| `quotes` | `list[dict]` — `supplier_id`, `issued_at`, `quote_valid_hours` | G8 |
| `shipment_confidence` | `dict[str, float]` — supplier_id to 0..1 | G9 |
| `unconfirmed_below` | float, default 0.5 | G9 |
| `claims` | `list[dict]` — legacy; CONTRADICTED reads as confidence 0.0 | G9 |
| `now` | `datetime \| str` | G8 |

### Branch on `vetoed()`, not on `passed`

```python
from guardrails.validator import validate, vetoed

verdict = validate(plan, context)
if vetoed(verdict):
    ...   # re-solve, max two rounds (§7 F-8)
elif verdict.forced_escalation:
    ...   # interrupt() — a human decides
```

`G2` and `G12` fail a plan for execution while re-solving is useless: an
over-threshold plan re-solves to itself, and G12 only fires after the ladder
has already run out. Treating either as a veto burns a correction round for
nothing. `G1`, `G8`, `G9` and an unjustified `G5` are real vetoes.

G9 keys on **shipment confidence**, not reputation. Its reason reads "claim
inconsistent with tracking evidence; units unconfirmed": the units are
excluded because they cannot be verified, which is not a finding against the
supplier.

**`vetoed()` says whether to recover, not how.** The recoveries differ, and
G8 is the one that is not a re-solve:

| rule | recovery |
|---|---|
| G1 | re-solve under a tighter cap |
| G5 | re-solve preserving safety stock, or record a justification |
| G9 | re-solve without those units — they cannot be confirmed |
| G8 | **fetch a fresh quote first**, then re-solve |

```python
if vetoed(verdict):
    if needs_fresh_quote(verdict):
        ...   # back to node 3: re-RFQ the named supplier
    ...       # then re-solve
```

Re-solving directly on G8 is permitted and terminates, but C7 forces
`y[s] = 0` for the expired supplier, so it is dropped for the rest of the run
when one RFQ would have restored it. `REQUOTE_RULES` and
`ESCALATE_ONLY_RULES` are exported for anyone building their own routing.

---

## `solve(inp) -> SolverOutput`

`from solver import solve` — CP-SAT. `solver.fallback.solve` is the greedy
insurance policy with the same signature.

`SolverInput` is built for you by `solver.build.build_solver_input(client,
component_id, budget_cap=..., contradicted=..., allow_reschedule=...)`, which
applies the G3/G4 pre-filters and reads `effective_reliability` from the
ledger. Take it or replace it, but keep both of those: passing
`reliability_score` instead of `effective_reliability` silently disables the
trust ledger.

`relaxation_used` tells you which rung answered: `none` → procurement only,
`reschedule` → production slipped, `partial` → coverage is incomplete.
`INFEASIBLE` always carries a `binding_constraint`, one of `budget`,
`deadline`, `available_quantity`, `moq`, `certification`.

---

## Known trap

`tests/solver/` shadows the `solver` package for a plain `python` invoked with
`tests/` as the working directory — `import solver` resolves to the test
directory as a namespace package. pytest is unaffected from any directory, and
so is any run from the repo root. `pip install -e .` is installed in the venv,
which makes every import work from an unrelated directory.
