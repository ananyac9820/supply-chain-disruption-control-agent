# Assumptions

Everything this system takes as given, with its source. **PS** means it comes
from the problem statement. **Ours** means we chose it, and the reasoning is
given so a judge can disagree with the number rather than with a mystery.

Every value marked *ours* is a single named constant, so correcting one is a
one-line change rather than a refactor.

## From the problem statement

| value | setting | source |
|---|---|---|
| Approval threshold | 150,000 | PS §5.2 `approval_required_above`, PS §5.8 |
| Approval comparison | strictly greater-than | PS §5.8: `approval_required = estimated_cost > 150000` — a plan costing exactly 150,000 executes autonomously |
| Permitted ERP actions | six, no others | PS §5.9 |
| Record shapes | verbatim | PS §5.1–5.10, reproduced in `contracts/models.py` without "improvement" |
| Catalog scale | 25 components, 14 suppliers, 30 POs, 8 production orders, 16 messages | PS §16 |
| Hidden test patterns | ten | PS §7 |
| Scope boundary | fully simulated | PS §18 |

## Ours, with reasoning

| constant | value | where | why |
|---|---|---|---|
| `EMERGENCY_BUDGET` | 400,000 | `contracts/constants.py` | PS §6 says the emergency budget is "limited" and never gives a number. 400,000 is ~2.7× the approval threshold, chosen so G1 and G2 bind at *different* times and both are demonstrable. If they were close, one would mask the other. |
| `TOOL_BUDGET_PER_DISRUPTION` | 15 | `contracts/constants.py` | PS §6 lists "tool calls may be limited" as a constraint with no figure. 15 is generous enough not to bind on a normal run and tight enough that the counter means something. If the organisers state a cap, set G10 to it. |
| `W_LATE` | 8,000 | `contracts/constants.py` | The business judgement that makes continuity dominate cost, matching the rubric's 35/20 split. A priority-weighted production day is worth roughly 8,000. Invented deliberately, and defensible out loud — which is worth more than an arbitrary weight nobody can explain. |
| `W_RISK` | 40 | `contracts/constants.py` | Per unit, per point of unreliability. Sized so a 0.15 reputation penalty shifts a ranking without swamping price. |
| `W_SPLIT` | 500 | `contracts/constants.py` | A mild preference for fewer suppliers. Large enough to break ties, small enough never to reject a genuinely better split. |
| `PRIORITY_WEIGHT` | high 5.0 / medium 2.0 / low 1.0 | `contracts/constants.py` | Not in the PS. Multiplies `W_LATE` so a high-priority day costs five times a low-priority one. |
| `MAX_DELAY_DAYS` | high 0 / medium 2 / low 5 | `contracts/constants.py` | Not in PS §5.4. Added so the reschedule lever has a bounded search. High-priority orders are immovable by construction. |
| `PLANNING_HORIZON_DAYS` | 14 | `agent/impact_math.py` | How far ahead the baseline counterfactual projects. Long enough to cover every seeded deadline, short enough that "production days lost" stays a number an ops manager recognises. |
| `CACHE_TTL_SIM_SECONDS` | 30 | `contracts/constants.py` | Read-cache staleness window. A second identical read inside the window is served from cache and logged as avoided. |
| Currency | ₹, implied | throughout | The PS uses bare numbers (118, 150000, 18000). We read them as rupees. Nothing depends on the symbol; money is integer paise inside the solver. |
| Money representation | integer paise | `solver/model.py` | CP-SAT is an integer solver. Floats in the objective produce a model that runs, returns a plausible answer, and is quietly wrong. Converted once at the boundary. |

## Seed data decisions

| decision | value | why |
|---|---|---|
| PROD-914 deadline | **2026-09-04** | The build document prints Sept 8. Corrected: PROD-914 must fall due *before* PROD-882 (Sept 6) or delaying it releases nothing for the high-priority order and the reschedule lever never binds. At Sept 8 the demo's central mechanism is unreachable. |
| COMP-104 catalog | five hand-authored rows | Sampled data would not contain a supplier that is cheapest *and* fastest *and* uncertified, so H-03 and H-05 would silently never fire and the agent would look right for the wrong reason. |
| Filler rows | not written | PS §16's counts are for realism. Every scenario-carrying row exists; the remainder is padding and no rubric line counts rows. |
| Simulated clock epoch | 2026-09-02T10:00 | Fixed so seeded deadlines land on the same day numbers on every run, which is what makes the demo reproducible. |
| Clock advance | one hour per message, one per RFQ | Gives quote expiry (G8) something real to measure against, and makes "the reply arrives on the next tick" mean something without a scheduler. |

## Interpretations of the specification

Two places where the build document, read literally, produces a system that is
wrong. Both are recorded rather than quietly patched.

**C6 is cumulative, not per-order.** As printed, C6 compares each production
order independently against the same `usable_stock`, which counts the same
units once per order. Under that reading two orders needing 700 each are both
satisfiable from 240 units of stock. Orders are walked in effective-deadline
order, each consuming what earlier ones left. Recorded in
[`solver/fixtures/README.md`](../solver/fixtures/README.md).

**G11 is a model constraint, not a pre-solve filter.** §4.4 classifies G3 and
G4 as pre-solve filters and G11 as a constraint. Enforcing G11 twice — once by
eliminating long-lead suppliers before the solve, once inside it — destroys
the evidence `_diagnose()` uses and makes a timing failure report as a
quantity shortage.

## Open questions for the organisers

1. Is the emergency budget a stated figure? We assume 400,000.
2. Is the tool-call budget a hard cap or a soft expectation? We assume 15 per
   disruption.
3. Is the sandbox provided, and do its record shapes match PS §5 exactly? We
   built our own against the PS shapes verbatim.
