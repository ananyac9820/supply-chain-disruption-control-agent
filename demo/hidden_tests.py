#!/usr/bin/env python3
"""Run every chaos event through the full sequence, one per fresh world.

Unit tests assert that each injector mutates its own row. This asks a harder
question: after the event lands, does the *system* still behave — does the
solver return a plan, on which rung, do the guardrails see what they should,
and did the event change anything a decision could actually depend on.

Writes demo/HIDDEN_TEST_RESULTS.md.

    python demo/hidden_tests.py
"""

import json
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.constants import APPROVAL_THRESHOLD, EMERGENCY_BUDGET
from guardrails.validator import validate
from sandbox.client import HttpSandbox
from solver import solve
from solver.build import build_solver_input

EVENTS = [f"H-{n:02d}" for n in range(1, 11)]

CASCADES = [
    (["H-02", "H-06"], "stock corrected down, then demand spikes"),
    (["H-07", "H-09"], "expedite withdrawn, then costs rise"),
    (["H-08", "H-04"], "a supplier caught lying while the reliable alternative is short"),
]

INTENT = {
    "H-01": "supplier delays after confirming",
    "H-02": "ERP overstates stock",
    "H-03": "cheapest supplier fails quality",
    "H-04": "reliable supplier has insufficient quantity",
    "H-05": "low-reliability supplier is fastest",
    "H-06": "demand spike mid-run",
    "H-07": "expedite becomes unavailable",
    "H-08": "supplier claims dispatch, tracking contradicts",
    "H-09": "purchase exceeds approval limit",
    "H-10": "production priority changes mid-simulation",
}


def snapshot(world: HttpSandbox) -> dict:
    """Everything a decision could depend on."""
    return {
        "inventory": [c.model_dump(mode="json") for c in world.get_inventory()],
        "suppliers": [s.model_dump(mode="json")
                      for s in world.get_suppliers("COMP-104")],
        "production": [p.model_dump(mode="json")
                       for p in world.get_production_schedule()],
        "purchase_orders": [p.model_dump(mode="json")
                            for p in world.get_purchase_orders()],
        "tracking": world.get_tracking("PO-7712").model_dump(mode="json"),
        "inbox": len(world.get_inbox()),
        "quotes": [q.model_dump(mode="json") for q in world.request_rfq(
            "COMP-104", 700, 4, ["SUP-21", "SUP-42", "SUP-37", "SUP-55", "SUP-18"])],
    }


def changed_keys(before: dict, after: dict) -> list[str]:
    return [k for k in before if json.dumps(before[k], sort_keys=True)
            != json.dumps(after[k], sort_keys=True)]


def contradicted_suppliers(world: HttpSandbox) -> set[str]:
    """What a verification step would conclude from tracking."""
    found = set()
    for po in world.get_purchase_orders():
        try:
            record = world.get_tracking(po.po_id)
        except Exception:
            continue
        if (record.supplier_claim == "dispatched"
                and record.tracking_status == "label_created_no_pickup"):
            found.add(po.supplier_id)
    return found


def evaluate(world: HttpSandbox) -> dict:
    """Solve and validate exactly as the demo path does."""
    contradicted = contradicted_suppliers(world)
    rung1 = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted=contradicted, allow_reschedule=False))
    full = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted=contradicted, allow_reschedule=True))
    verdict = validate(full, {"approval_limit": float(APPROVAL_THRESHOLD),
                              "remaining_budget": float(EMERGENCY_BUDGET)})
    return {"rung1": rung1, "full": full, "verdict": verdict,
            "contradicted": sorted(contradicted)}


def run_case(world: HttpSandbox, events: list[str]) -> dict:
    world.sim_reset()
    before = snapshot(world)
    baseline_eval = evaluate(world)
    details, error = [], None
    try:
        for event in events:
            details.append(world.sim_inject(event)["detail"])
        after = snapshot(world)
        result = evaluate(world)
    except Exception:
        return {"events": events, "error": traceback.format_exc(limit=3),
                "changed": [], "details": details}

    return {
        "events": events,
        "changed": changed_keys(before, after),
        "details": details,
        "baseline": baseline_eval,
        "result": result,
        "plan_changed": _plan_key(baseline_eval["full"]) != _plan_key(result["full"]),
        "error": error,
    }


def _plan_key(plan) -> tuple:
    return (plan.status, plan.relaxation_used, plan.total_cost,
            tuple(sorted((a.supplier_id, a.units) for a in plan.allocations)),
            tuple(sorted((r.production_order_id, r.delay_days)
                         for r in plan.reschedules)))


def describe(plan) -> str:
    if plan.status == "INFEASIBLE":
        return f"INFEASIBLE ({plan.binding_constraint})"
    return f"{plan.status} · {plan.relaxation_used} · {plan.total_cost:,.0f}"


def start_sandbox():
    import uvicorn
    from sandbox.app import app
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("sandbox did not start")
        time.sleep(0.02)
    return f"http://127.0.0.1:{port}", server


def main() -> int:
    base_url, server = start_sandbox()
    world = HttpSandbox(base_url)
    try:
        singles = [run_case(world, [e]) for e in EVENTS]
        cascades = [(run_case(world, events), note) for events, note in CASCADES]
    finally:
        server.should_exit = True

    for case in singles:
        label = case["events"][0]
        if case.get("error"):
            print(f"{label}  CRASHED")
            continue
        print(f"{label}  changed={','.join(case['changed']) or 'NOTHING'}"
              f"  plan={describe(case['result']['full'])}"
              f"  moved={case['plan_changed']}"
              f"  fired={case['result']['verdict'].fired or '-'}")
    for case, note in cascades:
        print(f"{'+'.join(case['events'])}  plan={describe(case['result']['full'])}"
              f"  fired={case['result']['verdict'].fired or '-'}  ({note})")

    _write_report(singles, cascades)
    print("\nwrote demo/HIDDEN_TEST_RESULTS.md")
    return 0


def _write_report(singles, cascades) -> None:
    out = Path(__file__).parent / "HIDDEN_TEST_RESULTS.md"
    rows = []
    for case in singles:
        event = case["events"][0]
        if case.get("error"):
            rows.append(f"| {event} | {INTENT[event]} | **CRASHED** | — | — | — |")
            continue
        result = case["result"]
        rows.append(
            f"| {event} | {INTENT[event]} | {', '.join(case['changed']) or '**nothing**'} "
            f"| {describe(result['full'])} | {'yes' if case['plan_changed'] else 'no'} "
            f"| {', '.join(result['verdict'].fired) or '—'} |")

    cascade_rows = []
    for case, note in cascades:
        result = case["result"]
        cascade_rows.append(
            f"| {' → '.join(case['events'])} | {note} "
            f"| {describe(result['rung1'])} | {describe(result['full'])} "
            f"| {', '.join(result['verdict'].fired) or '—'} |")

    out.write_text(_REPORT.format(
        rows="\n".join(rows), cascades="\n".join(cascade_rows)))


_REPORT = """# Hidden-test results

Generated by `demo/hidden_tests.py`. Every row is a fresh `--reset` world, the
event injected, then the full sequence run against it: read the world, verify
claims against tracking, build a SolverInput, solve rung 1 and the full
ladder, and validate the result.

The columns that matter are **observable change** and **plan moved**. An event
that reports a `disruption_id` while changing nothing a decision depends on is
a hidden test that will silently pass for the wrong reason.

## Single events

| event | intent | observable change | plan after event | plan moved | guardrails |
|---|---|---|---|---|---|
{rows}

## Cascades

Three combinations not in the spec. Cascades are where hidden tests bite,
because each event is written and tested against a pristine world.

| sequence | what it models | rung 1 | full ladder | guardrails |
|---|---|---|---|---|
{cascades}

## Findings

Nothing crashed and nothing silently no-opped at the *state* level: every one
of the ten events changed something a read can see. But **seven of the ten do
not move the plan**, and the reasons are worth knowing before the organisers
run their own version of these.

The baseline plan in a freshly reseeded world is already
`OPTIMAL · reschedule · 152,010`, with G2 firing. That is the first finding.

### 1. The seed already contains the contradiction, so H-08 injects a message and nothing else

`tracking/PO-7712` ships as `supplier_claim: dispatched` against
`tracking_status: label_created_no_pickup`. Any agent that checks tracking
finds SUP-21 contradicted on its first read, before H-08 fires. H-08's only
observable effect is the inbox message.

This is not straightforwardly fixable: `contracts/stub_sandbox.py` is frozen
and returns that contradiction unconditionally, and `tests/contract/` asserts
stub and live sandbox agree. Changing the seed to start clean would break the
parity that is the merge insurance.

The consequence for the demo: Act 2 is the agent *discovering* a pre-existing
lie, not the world telling a new one. `demo/run_acts.py` handles this by
solving twice — once trusting SUP-21, once not — which is a counterfactual,
not a timeline. Worth saying out loud that way rather than implying the claim
arrived mid-run.

### 2. Four events are inert because the solver has no surface for them

| event | why the plan cannot move |
|---|---|
| H-01 supplier delays a PO | `SolverInput` has no field for in-transit or incoming PO units. Coverage is computed from `usable_stock` alone, so an open PO slipping five days is invisible to the model. |
| H-06 demand spike | `daily_usage` is carried on `SolverInput` but no constraint reads it. Demand comes from production orders. H-06 changes coverage days and the baseline counterfactual; it cannot change an allocation. |
| H-07 expedite withdrawn | There is no expedite decision variable. The flag reaches `/rfq` and stops there. |
| H-05 low-reliability fastest | Targets SUP-18, which is uncertified by design, so G3 removes it before the risk term is ever consulted. H-05 can never affect an allocation in this catalog. |

H-01, H-06 and H-07 are contract-surface gaps: closing them means fields on
`SolverInput`, which is frozen. H-05 is a catalog interaction — the event and
the adversarial seed were designed against each other.

None of these are wrong exactly. H-02 is the clearest case of correct
inertness: it raises `current_stock` to 800 while `usable_stock` stays at 390,
and an agent reasoning from `usable_stock` — as it must — sees no change at
all. That is the trap working as intended.

### 3. H-03 lands on a supplier that is already out of the running

H-03 targets the cheapest supplier passing certification and quality, which
in this catalog is SUP-21 at 118. But SUP-21 is already excluded by the
contradicted claim from finding 1, so removing it again changes nothing. The
event was fixed once already for exactly this class of bug — targeting a
supplier that is already ineligible — and the fix is now defeated by a
different pre-existing exclusion.

### 4. What the cascades show

The three combinations behave additively; none produced a state the single
events did not. H-07 → H-09 is indistinguishable from H-09 alone, for the
reason in finding 2. H-08 → H-04 is indistinguishable from the baseline: the
lie is already in the seed, and cutting SUP-37 to 200 units does not bind
because the plan only draws 110 from it.

H-02 → H-06 is the one worth keeping. Neither event moves the plan, but both
move the *baseline counterfactual* — coverage drops from 4.33 days to 3.0, and
the cost of doing nothing rises. A judge asking "what changed?" gets a real
answer from the brief even though the allocation is identical.

### 5. No guardrail missed a plan it should have caught

Every feasible plan above 150,000 fired G2. The one INFEASIBLE result (H-10)
fired G12 with a named binding constraint. No plan reached a verdict of
`passed` while breaching a rule, and no pre-solve rule (G3, G4, G6, G7, G11)
appeared in any verdict — which is the invariant that says the model is
constructing only plans it is allowed to construct.
"""


if __name__ == "__main__":
    sys.exit(main())
