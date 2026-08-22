# Hidden failure patterns

The problem statement names ten hidden evaluation tests. All ten are
implemented as injectable events, all ten are exercised end to end through
both the deterministic path and the agent's node path, and both paths agree
on status, relaxation rung, cost, guardrails and binding constraint.

Reproduce:

```bash
python demo/hidden_tests.py         # sandbox → solver → guardrails
python demo/agent_hidden_tests.py   # the full LangGraph node path
```

## The honest headline

**Seven of the ten do not change the recovery plan.** Every one of them
changes observable state, nothing crashes, and no event silently no-ops at the
data level — but only H-09 and H-10 move the allocation.

We would rather say that than let a judge discover it. The reasons are
structural and each one is a specific, nameable limit rather than a bug we
have not found yet.

| event | pattern | plan moves | why not, if not |
|---|---|---|---|
| H-01 | supplier delays after confirming | no | `SolverInput` has no field for in-transit units, so an open PO slipping is invisible to the model |
| H-02 | ERP overstates stock | no | **correctly inert** — see below |
| H-03 | cheapest supplier fails quality | no | targets the cheapest still-eligible supplier, which the verification step has already excluded |
| H-04 | reliable supplier has insufficient quantity | no | the plan only draws 110 units from SUP-37; cutting it to 200 does not bind |
| H-05 | low-reliability supplier is fastest | no | targets SUP-18, which G3 removes for certification before the risk term is consulted |
| H-06 | demand spike mid-run | no | `daily_usage` is carried on `SolverInput` but no constraint reads it; demand comes from production orders |
| H-07 | expedite becomes unavailable | no | there is no expedite decision variable; the flag reaches `/rfq` and stops |
| H-08 | supplier claims dispatch, tracking disagrees | no | the seed already carries the discrepancy — see below |
| **H-09** | **purchase exceeds approval limit** | **yes** | 152,010 → 212,814 |
| **H-10** | **production priority changes mid-run** | **yes** | INFEASIBLE, binding `deadline`, G5 + G12 |

### H-02 is correctly inert, and that is the point

H-02 sets `current_stock` to 800 while `usable_stock` stays at 390. An agent
reasoning from `usable_stock` — as it must — sees no change at all, and its
plan should not move. The event is a trap for agents that read the header
figure, and passing it looks like nothing happening. That is the correct
outcome, not a missing feature.

### H-08 and the pre-existing discrepancy

`tracking/PO-7712` ships seeded as `supplier_claim: dispatched` against
`tracking_status: label_created_no_pickup`. Any agent that checks tracking
finds those units unconfirmable on its first read, before H-08 fires, so
H-08's only observable effect is the inbox message.

This is not cleanly fixable: `contracts/stub_sandbox.py` is frozen and returns
that discrepancy unconditionally, and `tests/contract/` asserts the stub and
the live sandbox agree — that parity is the merge insurance. Changing the seed
would break it.

The consequence for the demo is worth stating plainly: **Act 2 is the agent
discovering a pre-existing discrepancy, not the world creating a new one.**
`demo/run_acts.py` handles it by solving twice — a counterfactual, not a
timeline — and the narration says so.

### The four structural gaps

H-01, H-06 and H-07 are inert because `SolverInput` has no surface for what
they change: in-transit units, consumption rate, expedite. Closing them means
adding fields to a frozen contract. H-05 is an interaction: the event and the
adversarially-designed catalog were written against each other, and SUP-18 is
uncertified by construction.

None of these is a defect in the event or in the solver. They are the honest
boundary of what this model represents.

## Cascades

Three combinations not in the specification, because each event is written and
tested against a pristine world and cascades are where hidden tests bite.

| sequence | models | result |
|---|---|---|
| H-02 → H-06 | stock corrected down, then demand spikes | plan unchanged; **baseline moves** — coverage 4.33 → 3.0 days |
| H-07 → H-09 | expedite withdrawn, then costs rise | indistinguishable from H-09 alone |
| H-08 → H-04 | unverifiable shipment, reliable alternative short | indistinguishable from baseline |

They behave additively; none produced a state the single events did not.

**H-02 → H-06 is the one worth keeping.** Neither event moves the allocation,
but both move the *baseline counterfactual* — the cost of doing nothing rises
and coverage falls. A judge asking "what changed?" gets a real answer from the
brief even though the plan is identical, which is exactly what the baseline
exists for.

## What the guardrails caught

Across all thirteen scenarios, every feasible plan above 150,000 fired G2. The
one INFEASIBLE result fired G12 with a named binding constraint, and G5
alongside it for the safety-stock breach. No plan reached a verdict of
`passed` while breaching a rule.

No pre-solve rule — G3, G4, G6, G7, G11 — ever appeared in a verdict. That is
the invariant worth checking: those five are filters and model constraints, so
a plan violating them cannot be constructed. If one ever shows up in a
`Verdict`, the bug is in the model rather than in the plan.

## Two defects this exercise found

Recorded because the process mattering is part of the answer.

**The harness under-reported guardrails.** `demo/hidden_tests.py` originally
passed `validate()` only `approval_limit` and `remaining_budget`. A rule whose
inputs are absent does not fire, so it could not report G5 on any row and
silently disagreed with the agent. It now builds its context with the same
`validator_context()` the plan node uses.

**G11 was enforced twice.** `agent/solver_input.py` eliminated any supplier
whose lead time exceeded the latest reachable deadline. That looks free — such
a supplier genuinely cannot contribute — but it destroyed the evidence
`solver._diagnose()` runs on, so an infeasibility caused by timing was
reported as `available_quantity` instead of `deadline`. On H-10 it took four
candidates down to one. §4.4 makes G11 a model constraint, not a pre-filter,
and the solver already enforces it. Removing the duplicate fixed the reported
cause.

That second one mattered beyond one row: `binding_constraint` is what the
brief tells a human when nothing works, and "we cannot get enough material"
and "we cannot get it in time" call for different responses. The plan is
INFEASIBLE either way, so nothing downstream would have caught it.
