#!/usr/bin/env python3
"""Acts 1-3 of the demo, end to end, as a repeatable harness.

Not the pitch. This exists so the demo path can be run on demand and so
anything that only breaks *in sequence* — state left behind by an earlier
act, a chaos event that undoes an earlier one, a plan that stops crossing the
approval threshold once prices move — gets caught by a command rather than by
an audience.

Every act ends in checks. The script exits non-zero if any of them fail, so
it can go in front of a rehearsal or into CI.

Right now this drives the sandbox and the solver directly. After the hour-12
merge, Person B replaces the marked sections with the agent: the reads become
tool calls through the ledger, and the plan comes out of node 4 rather than
out of solve() called here.

Ownership note: §2.2 puts demo/ at "joint, hour 18+". This lands early because
it is a Track A test harness that happens to live here. Person B should feel
free to restructure it when the agent goes in.

    python demo/run_demo.py                 # starts its own sandbox
    python demo/run_demo.py --base-url http://localhost:8000
"""

import argparse
import socket
import sys
import threading
import time
from pathlib import Path

# pyproject's pythonpath is a pytest setting; a plain script needs this.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.constants import APPROVAL_THRESHOLD, EMERGENCY_BUDGET
from guardrails.validator import validate
from sandbox.client import HttpSandbox
from solver import solve
from solver.build import baseline, build_solver_input
from trust import effective_reliability, trust_read, trust_write

RULE = "─" * 78
FAILURES: list[str] = []


# ---- output ------------------------------------------------------------

def act(number: int, title: str) -> None:
    print(f"\n{RULE}\nACT {number} — {title}\n{RULE}")


def step(text: str) -> None:
    print(f"\n▸ {text}")


def line(text: str = "") -> None:
    print(f"   {text}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    mark = "✓" if condition else "✗"
    print(f"   {mark} {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(label)
    return condition


def show_plan(plan) -> None:
    line(f"status {plan.status} · relaxation {plan.relaxation_used}"
         f" · cost {plan.total_cost:,.2f}"
         f" · approval {'REQUIRED' if plan.requires_approval else 'not required'}")
    for a in plan.allocations:
        line(f"    {a.supplier_id}  {a.units:>5} units  @day {a.arrival_day}"
             f"  {a.cost:>12,.2f}")
    for r in plan.reschedules:
        line(f"    delay {r.production_order_id} by {r.delay_days} days")
    if plan.binding_constraint:
        line(f"    binding constraint: {plan.binding_constraint}")


# ---- acts --------------------------------------------------------------

def act_one(world: HttpSandbox) -> None:
    """Baseline disruption: SUP-21 delays PO-7712 for COMP-104."""
    act(1, "a supplier delay, and the cost of doing nothing")

    step("Inject H-01 — SUP-21 pushes PO-7712 out five days")
    world.sim_inject("H-01")
    po = world.get_purchase_orders("PO-7712")[0]
    line(f"PO-7712 now expects {po.expected_delivery}, status {po.status}")

    step("Read inventory — reason from usable_stock, never the header figure")
    component = world.get_inventory("COMP-104")[0]
    line(f"current_stock {component.current_stock} · usable_stock "
         f"{component.usable_stock} · daily_usage {component.daily_usage}")
    coverage = component.usable_stock / component.daily_usage
    line(f"coverage {coverage:.2f} days")
    check("usable_stock is below the header figure", 
          component.usable_stock < component.current_stock,
          f"{component.usable_stock} usable vs {component.current_stock} reported")

    step("Compute the cost of no action, before spending anything")
    base = baseline(world, "COMP-104")
    line(f"units short {base['units_short']} · production days lost "
         f"{base['production_days_lost']} · deadline misses "
         f"{', '.join(base['deadline_misses']) or 'none'}")
    check("doing nothing misses a deadline", bool(base["deadline_misses"]),
          "otherwise there is nothing to recover from")

    step("Ask the incumbent what is happening")
    world.send_message("SUP-21", "PO-7712 revised date",
                       "Please confirm the revised delivery date and quantity?")
    replies = [m for m in world.get_inbox() if m.sender.startswith("sup21")]
    line(f'reply: "{replies[-1].body[:70]}…"')

    step("Quote the alternates and solve")
    world.request_rfq("COMP-104", 700, 4, ["SUP-42", "SUP-37", "SUP-55", "SUP-18"])
    plan = solve(build_solver_input(world, "COMP-104", budget_cap=EMERGENCY_BUDGET))
    show_plan(plan)

    check("a recovery plan exists", plan.status != "INFEASIBLE")
    check("SUP-18 is absent — cheapest and fastest, uncertified",
          "SUP-18" not in {a.supplier_id for a in plan.allocations})

    step("Write the outcome back to the simulated ERP")
    result = world.erp_update("mark_po_delayed", {"po_id": "PO-7712"})
    line(f"erp_update -> {result['status']} {result['record_id']}")
    check("the ERP write landed", result["status"] == "ok")
    return plan


def act_two(world: HttpSandbox, act_one_plan) -> None:
    """The vague reply, the false claim, and a trust score that bites."""
    act(2, "a vague reply, a false claim, and a score that changes the answer")

    step("SUP-55 is asked for status, vaguely")
    world.send_message("SUP-55", "PO status", "Any update on this order?")
    vague = [m for m in world.get_inbox() if m.sender.startswith("sup55")][-1]
    line(f'reply 1: "{vague.body}"')
    check("reply 1 commits to no date and no quantity",
          "2026-" not in vague.body and not any(c.isdigit() for c in vague.body))

    step("Challenge it — a question naming a date and a quantity")
    world.send_message("SUP-55", "PO status",
                       "That is not actionable. Confirm the exact date and quantity?")
    specific = [m for m in world.get_inbox() if m.sender.startswith("sup55")][-1]
    line(f'reply 2: "{specific.body}"')
    check("the challenge produces specifics", "2026-" in specific.body)

    step("Inject H-08 — SUP-21 claims dispatch")
    world.sim_inject("H-08")
    claim = [m for m in world.get_inbox() if m.sender.startswith("sup21")][-1]
    line(f'claim: "{claim.body[:64]}…"')

    step("Verify the claim against tracking, which is ground truth")
    tracking = world.get_tracking("PO-7712")
    line(f"supplier_claim {tracking.supplier_claim!r} · tracking_status "
         f"{tracking.tracking_status!r} · last_movement {tracking.last_movement}")
    contradicted = (tracking.supplier_claim == "dispatched"
                    and tracking.tracking_status == "label_created_no_pickup")
    check("the claim is CONTRADICTED by tracking", contradicted)

    step("Decrement SUP-21's trust, and re-solve with what we now know")
    catalog = {s.supplier_id: s for s in world.get_suppliers("COMP-104")}
    before_score = effective_reliability("SUP-21", catalog["SUP-21"].reliability_score)
    trust_write("SUP-21", "contradicted_claim")
    after_score = effective_reliability("SUP-21", catalog["SUP-21"].reliability_score)
    line(f"SUP-21 effective reliability {before_score:.2f} -> {after_score:.2f} "
         f"({trust_read('SUP-21').contradicted_claims} contradicted claim)")
    check("the trust score actually moved", after_score < before_score)

    plan = solve(build_solver_input(world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
                                    contradicted={"SUP-21"}))
    show_plan(plan)
    check("SUP-21 no longer carries any of the plan",
          "SUP-21" not in {a.supplier_id for a in plan.allocations})
    check("the plan changed because of what was learned",
          {a.supplier_id for a in plan.allocations}
          != {a.supplier_id for a in act_one_plan.allocations},
          "same catalog, same gap, different answer")
    return plan


def act_three(world: HttpSandbox) -> None:
    """Infeasible without rescheduling, and over the approval threshold."""
    act(3, "no sourcing plan exists, and the one that does needs a human")

    step("Inject H-09 — the alternates raise prices 40%")
    world.sim_inject("H-09")
    for s in sorted(world.get_suppliers("COMP-104"), key=lambda s: s.supplier_id):
        line(f"{s.supplier_id}  {s.unit_price:>8,.2f}  lead {s.lead_time_days}d")

    step("Solve procurement-only — production reschedule forbidden")
    procurement_only = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted={"SUP-21"}, allow_reschedule=False))
    show_plan(procurement_only)
    check("no sourcing-only plan exists",
          procurement_only.status == "INFEASIBLE")
    check("and it says which constraint bound",
          procurement_only.binding_constraint is not None,
          procurement_only.binding_constraint or "")

    step("Solve again with production rescheduling allowed")
    plan = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted={"SUP-21"}, allow_reschedule=True))
    show_plan(plan)
    check("rescheduling makes it feasible", plan.status != "INFEASIBLE")
    check("and the relaxation is reported honestly",
          plan.relaxation_used == "reschedule")
    check("the low-priority order absorbs the slip",
          any(r.production_order_id == "PROD-914" for r in plan.reschedules))

    step("Check the approval threshold")
    approval = world.check_approval("create_alternate_po", plan.total_cost)
    line(f"{plan.total_cost:,.2f} against a threshold of {APPROVAL_THRESHOLD:,} "
         f"-> approval {'required' if approval.approval_required else 'not required'}")
    line(f"reason: {approval.approval_reason}")
    check("the plan crosses the threshold", approval.approval_required,
          "Act 3 has nothing to escalate otherwise")

    step("Run the guardrails")
    verdict = validate(plan, {"approval_limit": float(APPROVAL_THRESHOLD),
                              "remaining_budget": float(EMERGENCY_BUDGET)})
    line(f"fired {verdict.fired or 'nothing'} · forced_escalation "
         f"{verdict.forced_escalation}")
    for reason in verdict.reasons:
        line(f"    {reason}")
    check("G2 blocks autonomous execution", "G2" in verdict.fired)
    check("and forces escalation", verdict.forced_escalation)

    step("Record the escalation in the simulated ERP")
    result = world.erp_update("record_escalation", {
        "component_id": "COMP-104", "total_cost": plan.total_cost,
        "reason": approval.approval_reason})
    check("the escalation was recorded", result["status"] == "ok",
          result["record_id"] or "")


# ---- harness -----------------------------------------------------------

def start_sandbox() -> tuple[str, object]:
    """Run the sandbox in this process so the harness needs no setup."""
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="an already-running sandbox")
    args = parser.parse_args()

    server = None
    if args.base_url:
        base_url = args.base_url
    else:
        base_url, server = start_sandbox()

    world = HttpSandbox(base_url)
    world.sim_reset()
    print(f"sandbox {base_url} · simulated clock {world.sim_clock()['now']}")

    try:
        first = act_one(world)
        second = act_two(world, first)
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
    print("All checks held. The demo path runs end to end.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
