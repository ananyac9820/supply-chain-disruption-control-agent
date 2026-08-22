#!/usr/bin/env python3
"""The four-act demo, timed, with a marker at every act boundary.

    python demo/rehearse.py                 # rehearse against a clock
    python demo/rehearse.py --backup        # also write demo/BACKUP_RUN.txt

Acts 1-3 are demo/run_acts.py unchanged — one implementation, so the thing we
rehearse is the thing we demo. Act 4 is the live injection a judge triggers.

The markers exist so a rehearsal can be timed against a script. Machine time
is a fraction of the budget; the twelve minutes are narration, and the act
boundaries are where a presenter needs to know whether they are ahead or
behind.

ACT 4 fires H-10 or H-09 (default H-10). Those are the only two events of the
ten that move the plan — the other eight change observable state without
changing the recovery, which is honest but is an anticlimax on stage. See
docs/HIDDEN_FAILURES.md.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts.constants import APPROVAL_THRESHOLD, EMERGENCY_BUDGET
from guardrails.validator import validate
from sandbox.client import HttpSandbox
from solver import solve
from solver.build import build_solver_input, unconfirmed_shipment_suppliers

import run_acts
from run_acts import (RULE, act, check, line, show_plan, start_sandbox, step)

MARKER = "═" * 78


def marker(number: int, title: str, started: float) -> None:
    elapsed = time.monotonic() - started
    print(f"\n{MARKER}")
    print(f"◆ ACT {number} BOUNDARY — {title}")
    print(f"◆ elapsed {int(elapsed // 60)}:{elapsed % 60:04.1f}")
    print(MARKER)


def act_four(world: HttpSandbox, event: str) -> None:
    """The live injection. A judge picks the moment; the plan reforms."""
    act(4, f"a live injection — {event}, fired mid-demo")

    step(f"Before: the plan currently on the table")
    before = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted=unconfirmed_shipment_suppliers(world), allow_reschedule=True))
    show_plan(before)

    step(f"Inject {event} — nobody typed this in advance")
    detail = world.sim_inject(event)["detail"]
    for key, value in detail.items():
        line(f"{key}: {value}")

    step("The world is re-read and the plan re-solved")
    after = solve(build_solver_input(
        world, "COMP-104", budget_cap=EMERGENCY_BUDGET,
        contradicted=unconfirmed_shipment_suppliers(world), allow_reschedule=True))
    show_plan(after)

    verdict = validate(after, {"approval_limit": float(APPROVAL_THRESHOLD),
                               "remaining_budget": float(EMERGENCY_BUDGET),
                               "projected_stock": 0, "safety_stock": 150,
                               "affected_priorities": ["high"]})
    line("")
    line(f"guardrails: {', '.join(verdict.fired) or 'nothing'} · "
         f"forced_escalation {verdict.forced_escalation}")
    for reason in verdict.reasons:
        line(f"    {reason}")

    check("the injection changed the plan",
          (after.status, after.total_cost) != (before.status, before.total_cost),
          f"{before.status} {before.total_cost:,.0f} -> "
          f"{after.status} {after.total_cost:,.0f}")
    if after.status == "INFEASIBLE":
        check("and it names what bound, not just that it failed",
              after.binding_constraint is not None, after.binding_constraint or "")
        check("the guardrails escalate rather than execute",
              verdict.forced_escalation)
    else:
        check("the guardrails see the new cost", "G2" in verdict.fired)


class Tee:
    """Write to the terminal and to the backup file at once.

    The backup has to be a transcript of a real run, not a re-run: if the
    live demo fails, the thing shown as a fallback should be the run that was
    actually rehearsed.
    """

    def __init__(self, stream, path: Path) -> None:
        self.stream = stream
        self.handle = path.open("w")

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.handle.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()

    def __getattr__(self, name):
        """Everything else is the real stream's business.

        uvicorn configures logging against sys.stdout and asks it for isatty,
        fileno, encoding and more. A Tee that only implements write and flush
        fails that configuration with an unrelated-looking error.
        """
        return getattr(self.stream, name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default="H-10", choices=["H-10", "H-09"],
                    help="the live injection for Act 4")
    ap.add_argument("--backup", action="store_true",
                    help="also write demo/BACKUP_RUN.txt")
    args = ap.parse_args()

    backup_path = Path(__file__).parent / "BACKUP_RUN.txt"
    tee = None
    if args.backup:
        tee = Tee(sys.stdout, backup_path)
        sys.stdout = tee

    base_url, server = start_sandbox()
    world = HttpSandbox(base_url)
    world.sim_reset()

    started = time.monotonic()
    timings = []
    print(f"REHEARSAL · simulated clock {world.sim_clock()['now']} · "
          f"Act 4 injection {args.event}")

    try:
        for number, (title, fn) in enumerate([
            ("the disruption and the cost of doing nothing", run_acts.act_one),
            ("the incident, and what the evidence does not say", run_acts.act_two),
            ("infeasible, then rescheduled, then escalated", run_acts.act_three),
        ], start=1):
            act_started = time.monotonic()
            fn(world)
            timings.append((number, title, time.monotonic() - act_started))
            marker(number, title, started)

        act_started = time.monotonic()
        act_four(world, args.event)
        timings.append((4, f"live injection ({args.event})",
                        time.monotonic() - act_started))
        marker(4, f"live injection ({args.event})", started)
    finally:
        server.should_exit = True

    total = time.monotonic() - started
    print(f"\n{RULE}\nTIMING\n{RULE}")
    for number, title, seconds in timings:
        print(f"   Act {number}  {seconds:>6.1f}s   {title}")
    print(f"   {'total':<7}{total:>6.1f}s")
    print()
    print("   Machine time only. The twelve-minute budget is narration; these")
    print("   numbers say the software will never be what makes you late.")

    failures = run_acts.FAILURES
    print(f"\n{RULE}")
    if failures:
        print(f"FAILED — {len(failures)} check(s) did not hold:")
        for failure in failures:
            print(f"  ✗ {failure}")
        if tee is not None:
            sys.stdout = tee.stream
            tee.close()
        return 1
    print("All checks held across all four acts.")
    if tee is not None:
        sys.stdout = tee.stream
        tee.close()
        print(f"\nbackup written to {backup_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
