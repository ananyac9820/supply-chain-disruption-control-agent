#!/usr/bin/env python3
"""Acts 1-3 against the sandbox and solver directly — the demo harness.

The only one. An earlier second harness (run_demo.py) was folded in here and
deleted: it had drifted behind this one, and a superseded harness that still
encodes a rejected behaviour is worse than no harness if someone opens it
during judging.

Person B wires the agent in after the merge. For now this proves the *world*
behaves correctly in sequence — the class of bug that unit tests cannot see,
because it only appears when one act leaves state behind for the next.

    python demo/run_acts.py --reset

--reset reseeds first, and is what makes the run repeatable: the simulated
clock starts from a fixed epoch, so a reset run prints byte-identical output
every time. Without it the world carries over from the previous run and the
numbers move, which is the sandbox working, not a fault.

Note on casting: §4.8 seeds SUP-21 as the *contradictory* persona and SUP-55
as the *vague* one. The vague beat therefore uses SUP-55; SUP-21 carries the
dispatch claim. One supplier cannot be both without changing the seed, and
the seed is the test fixture.
"""

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.constants import APPROVAL_THRESHOLD, EMERGENCY_BUDGET
from guardrails.validator import validate
from sandbox.client import HttpSandbox
from solver import solve
from solver.build import (baseline, build_solver_input,
                          unconfirmed_shipment_suppliers)
from trust import (effective_reliability, incidents_for, reputation,
                   shipment_confidence)

RULE = "─" * 78
FAILURES: list[str] = []


def act(number: int, title: str) -> None:
    print(f"\n{RULE}\nACT {number} — {title}\n{RULE}")


def step(text: str) -> None:
    print(f"\n▸ {text}")


def line(text: str = "") -> None:
    print(f"   {text}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    print(f"   {'✓' if condition else '✗'} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def show_plan(plan, indent: str = "    ") -> None:
    line(f"{indent}status {plan.status} · rung {plan.relaxation_used}"
         f" · cost {plan.total_cost:,.2f}"
         f" · approval {'REQUIRED' if plan.requires_approval else 'not required'}")
    for a in plan.allocations:
        line(f"{indent}  {a.supplier_id}  {a.units:>5} units  @day {a.arrival_day}"
             f"  {a.cost:>12,.2f}")
    for r in plan.reschedules:
        line(f"{indent}  delay {r.production_order_id} by {r.delay_days} days")
    if plan.binding_constraint:
        line(f"{indent}  binding constraint: {plan.binding_constraint}")


# ---- Act 1 --------------------------------------------------------------

def act_one(world: HttpSandbox) -> None:
    act(1, "SUP-21 delays PO-7712")

    step("Inject H-01")
    detail = world.sim_inject("H-01")["detail"]
    line(f"PO-7712 expected_delivery {detail['was']} -> {detail['expected_delivery']}")

    step("Read inventory — reason from usable_stock, never the header figure")
    c = world.get_inventory("COMP-104")[0]
    line(f"current_stock {c.current_stock} · usable_stock {c.usable_stock}"
         f" · safety_stock {c.safety_stock} · daily_usage {c.daily_usage}")
    line(f"coverage {c.usable_stock / c.daily_usage:.2f} days"
         f"   (usable, not the {c.current_stock / c.daily_usage:.2f} the header implies)")
    check("the header overstates what is usable", c.usable_stock < c.current_stock)

    step("Read the production schedule")
    orders = world.get_production_schedule()
    for p in orders:
        line(f"{p.production_order_id}  {p.units_planned:>4} units"
             f"  due {p.deadline}  {p.priority:<6} max_delay {p.max_delay_days}d")
    check("the delayable order falls due first",
          min(orders, key=lambda p: p.deadline).max_delay_days > 0,
          "otherwise rescheduling frees nothing")

    step("Cumulative shortfall — orders consume stock in deadline order")
    base = baseline(world, "COMP-104")
    on_hand = c.usable_stock - c.safety_stock
    line(f"on hand above safety stock: {c.usable_stock} - {c.safety_stock} = {on_hand}")
    running = on_hand
    for p in sorted(orders, key=lambda p: p.deadline):
        need = p.units_planned * p.component_required_per_unit
        covered = max(0, min(need, running))
        running -= covered
        line(f"{p.production_order_id} needs {need:>4} · covered {covered:>4}"
             f" · short {need - covered:>4}")
    line(f"cumulative shortfall {base['units_short']} units"
         f" · {base['production_days_lost']} production days"
         f" · misses {', '.join(base['deadline_misses'])}")
    check("doing nothing misses a deadline", bool(base["deadline_misses"]))

    step("Quote the alternates and solve")
    world.request_rfq("COMP-104", 700, 4, ["SUP-42", "SUP-37", "SUP-55", "SUP-18"])
    plan = solve(build_solver_input(world, "COMP-104", budget_cap=EMERGENCY_BUDGET))
    show_plan(plan)
    check("a recovery plan exists", plan.status != "INFEASIBLE")
    check("SUP-18 carries none of it — cheapest and fastest, uncertified",
          "SUP-18" not in {a.supplier_id for a in plan.allocations})

    step("Write the outcome back to the simulated ERP")
    result = world.erp_update("mark_po_delayed", {"po_id": "PO-7712"})
    line(f"erp_update -> {result['status']} {result['record_id']}")
    check("the ERP write landed", result["status"] == "ok")


# ---- Act 2 --------------------------------------------------------------

def act_two(world: HttpSandbox) -> None:
    act(2, "a vague reply, a dispatch claim, and evidence that does not support it")

    step("SUP-55 (vague persona) is asked for status")
    world.send_message("SUP-55", "PO status", "Any update on this order?")
    first = [m for m in world.get_inbox() if m.sender.startswith("sup55")][-1]
    line(f'"{first.body}"')
    check("no date and no quantity",
          "2026-" not in first.body and not any(ch.isdigit() for ch in first.body))

    step("Follow up with a question naming a date and a quantity")
    world.send_message("SUP-55", "PO status",
                       "Not actionable. Confirm the exact date and quantity?")
    second = [m for m in world.get_inbox() if m.sender.startswith("sup55")][-1]
    line(f'"{second.body}"')
    check("the challenge produces specifics", "2026-" in second.body)

    step("SUP-21 (contradictory persona) is asked about PO-7712")
    world.send_message("SUP-21", "PO-7712", "Has this shipped?")
    claim = [m for m in world.get_inbox() if m.sender.startswith("sup21")][-1]
    line(f'"{claim.body}"')
    check("SUP-21 claims dispatch", "dispatched" in claim.body.lower())

    step("Check tracking — ground truth, and record what the evidence shows")
    catalog = {s.supplier_id: s for s in world.get_suppliers("COMP-104")}
    before = effective_reliability("SUP-21", catalog["SUP-21"].reliability_score)

    tracking = world.get_tracking("PO-7712")
    line(f"supplier_claim {tracking.supplier_claim!r}"
         f" · tracking_status {tracking.tracking_status!r}"
         f" · last_movement {tracking.last_movement}")

    after = effective_reliability("SUP-21", catalog["SUP-21"].reliability_score)

    line("")
    for inc in incidents_for("PO-7712"):
        line(f"incident {inc['incident_id']}  attribution {inc['attribution']}")
        line(f"    observed: {inc['observed']}")
        line(f"    expected: {inc['expected']}")
        line(f"    basis:    {inc['attribution_basis']}")
    line("")
    line(f"shipment_confidence(PO-7712)  1.00 -> {shipment_confidence('PO-7712'):.2f}"
         f"   these units cannot be counted")
    line(f"reputation (effective_reliability)  {before:.2f} -> {after:.2f}"
         f"   unchanged — the evidence does not attribute this to the supplier")

    check("the claim is not supported by tracking",
          tracking.supplier_claim == "dispatched"
          and tracking.tracking_status == "label_created_no_pickup")
    check("an incident is recorded regardless of fault",
          len(incidents_for("PO-7712")) == 1)
    check("the incident is UNATTRIBUTED",
          incidents_for("PO-7712")[0]["attribution"] == "UNATTRIBUTED",
          "a label with no pickup fits both a supplier that never tendered "
          "and a courier that never came")
    check("shipment confidence drops", shipment_confidence("PO-7712") < 1.0)
    check("reputation does not move", after == before,
          "no one is penalised on ambiguous evidence")
    check("and the catalog score is untouched",
          catalog["SUP-21"].reliability_score == 0.72)

    step("Both scores are visible side by side on GET /suppliers")
    for row in world.get_suppliers_with_trust("COMP-104"):
        line(f"{row['supplier_id']}  catalog {row['reliability_score']:.2f}"
             f"  effective {row['effective_reliability']:.2f}")


# ---- Act 3 --------------------------------------------------------------

def act_three(world: HttpSandbox) -> None:
    act(3, "what an unverifiable shipment costs, and why a human is now needed")

    def solve_both(contradicted: set[str]):
        rung1 = solve(build_solver_input(
            world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
            contradicted=contradicted, allow_reschedule=False))
        full = solve(build_solver_input(
            world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
            contradicted=contradicted, allow_reschedule=True))
        return rung1, full

    step("Solve counting SUP-21's units as confirmed")
    trusting_rung1, trusting = solve_both(set())
    line("rung 1, procurement only:")
    show_plan(trusting_rung1)
    line("full ladder:")
    show_plan(trusting)

    step("Solve with those units unconfirmed — the evidence does not support them")
    verified_rung1, verified = solve_both({"SUP-21"})
    line("rung 1, procurement only:")
    show_plan(verified_rung1)
    line("full ladder:")
    show_plan(verified)

    step("What changed")
    check("rung 1 is INFEASIBLE either way",
          trusting_rung1.status == "INFEASIBLE" and verified_rung1.status == "INFEASIBLE",
          f"binding {trusting_rung1.binding_constraint} / "
          f"{verified_rung1.binding_constraint}")
    check("both land on a 4-day PROD-914 delay",
          {(r.production_order_id, r.delay_days) for r in trusting.reschedules}
          == {("PROD-914", 4)}
          and {(r.production_order_id, r.delay_days) for r in verified.reschedules}
          == {("PROD-914", 4)},
          "the reschedule lever is not what moved")

    delta = verified.total_cost - trusting.total_cost
    line("")
    line(f"units counted     {trusting.total_cost:>12,.2f}"
         f"   approval {'required' if trusting.requires_approval else 'not required'}")
    line(f"units unconfirmed {verified.total_cost:>12,.2f}"
         f"   approval {'required' if verified.requires_approval else 'not required'}")
    line(f"                  {'-' * 12}")
    line(f"cost of the gap   {delta:>12,.2f}")
    line("")
    line(f"That delta is the causal link: finding that PO-7712's units cannot be")
    line(f"confirmed pushes the plan from {trusting.total_cost:,.0f} to"
         f" {verified.total_cost:,.0f}, across the {APPROVAL_THRESHOLD:,} threshold.")
    line(f"It is the verification step that creates the approval decision —")
    line(f"not a judgement about the supplier, whose reputation has not moved.")

    check("the cost moves by 8,690", round(delta, 2) == 8690.00, f"{delta:,.2f}")
    check("counting the units keeps it below the threshold",
          not trusting.requires_approval)
    check("excluding them crosses it", verified.requires_approval)

    step("Guardrails on each plan")
    context = {"approval_limit": float(APPROVAL_THRESHOLD),
               "remaining_budget": float(EMERGENCY_BUDGET)}
    trusting_verdict = validate(trusting, context)
    verified_verdict = validate(verified, context)
    line(f"units counted: fired {trusting_verdict.fired or 'nothing'}"
         f" · forced_escalation {trusting_verdict.forced_escalation}")
    line(f"unconfirmed:   fired {verified_verdict.fired or 'nothing'}"
         f" · forced_escalation {verified_verdict.forced_escalation}")
    for reason in verified_verdict.reasons:
        line(f"    {reason}")
    check("G2 does not fire when the units are counted",
          "G2" not in trusting_verdict.fired)
    check("G2 fires when they are excluded", "G2" in verified_verdict.fired)
    check("and forces escalation", verified_verdict.forced_escalation)

    step("Coda — H-09 raises the alternates 40% on top of all this")
    world.sim_inject("H-09")
    escalated = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted=unconfirmed_shipment_suppliers(world), allow_reschedule=True))
    show_plan(escalated)
    approval = world.check_approval("create_alternate_po", escalated.total_cost)
    line(f"{escalated.total_cost:,.2f} against a threshold of "
         f"{APPROVAL_THRESHOLD:,} -> approval "
         f"{'required' if approval.approval_required else 'not required'}")
    line(f"reason: {approval.approval_reason}")
    check("still feasible once costs rise", escalated.status != "INFEASIBLE")
    check("/approval/check agrees with the solver",
          approval.approval_required == escalated.requires_approval)

    step("Record the escalation in the simulated ERP")
    recorded = world.erp_update("record_escalation", {
        "component_id": "COMP-104", "total_cost": escalated.total_cost,
        "reason": approval.approval_reason})
    line(f"erp_update -> {recorded['status']} {recorded['record_id']}")
    check("the escalation was recorded", recorded["status"] == "ok")


# ---- harness ------------------------------------------------------------

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="reseed first; required for identical output")
    parser.add_argument("--base-url", help="an already-running sandbox")
    args = parser.parse_args()

    server = None
    base_url = args.base_url
    if base_url is None:
        base_url, server = start_sandbox()

    world = HttpSandbox(base_url)
    if args.reset:
        world.sim_reset()
    # The port is deliberately not printed: it is ephemeral, and printing it
    # would make two identical runs differ on line one.
    print(f"simulated clock {world.sim_clock()['now']}"
          f" · {'reseeded' if args.reset else 'continuing from existing state'}")

    try:
        act_one(world)
        act_two(world)
        act_three(world)
    finally:
        if server is not None:
            server.should_exit = True

    print(f"\n{RULE}")
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} check(s) did not hold:")
        for failure in FAILURES:
            print(f"  ✗ {failure}")
        return 1
    print("All checks held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
