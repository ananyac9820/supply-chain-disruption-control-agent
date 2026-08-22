#!/usr/bin/env python3
"""The ten chaos events through the LangGraph node path, not the harness.

demo/hidden_tests.py drives sandbox -> solver -> guardrails directly. This
drives the actual agent: monitor, impact, investigate, plan, gate, execute,
with the tool ledger, assumption register and escalation gate in the loop.
Person A's deterministic table is the reference; anything that differs is a
finding about orchestration, not a number to reconcile.

Each event gets a fresh database, a fresh sandbox reseed, a fresh tool ledger,
a reset agent clock and its own checkpointer thread, so nothing carries over.

    python demo/agent_hidden_tests.py
"""

from __future__ import annotations

import io
import json
import socket
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.types import Command

from sandbox.client import HttpSandbox

EVENTS = [f"H-{n:02d}" for n in range(1, 11)]

# Person A's deterministic-harness table, from demo/HIDDEN_TEST_RESULTS.md.
REFERENCE = {
    "H-01": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-02": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-03": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-04": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-05": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-06": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-07": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-08": ("OPTIMAL", "reschedule", 152010.0, ["G2"], None),
    "H-09": ("OPTIMAL", "reschedule", 212814.0, ["G2"], None),
    "H-10": ("INFEASIBLE", None, 0.0, ["G12", "G5"], "deadline"),
}


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


def run_event(world: HttpSandbox, event: str, index: int) -> dict:
    """One event, one clean agent run."""
    from agent import clock, tools
    from agent.audit import close_log, open_log
    from agent.graph import build_graph, initial_state
    from agent.ledger import reset_ledger

    disruption_id = f"DIS-{index:03d}"
    world.sim_reset()
    tools.set_sandbox(world)
    clock.reset()
    reset_ledger(disruption_id)

    injected = world.sim_inject(event)["detail"]

    log = open_log(disruption_id, path=f"/tmp/agent-{event}.jsonl")
    graph = build_graph()
    config = {"configurable": {"thread_id": f"{event}-{index}"}}

    paused = False
    try:
        # The agent narrates to stdout; capture it so the table stays readable.
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = graph.invoke(initial_state(disruption_id), config)
            if "__interrupt__" in result:
                paused = True
                result = graph.invoke(Command(resume={"decision": "approve"}), config)
    except Exception:
        return {"event": event, "error": traceback.format_exc(limit=4),
                "injected": injected}
    finally:
        close_log(disruption_id)

    plan = result.get("plan") or {}
    verdicts = result.get("guardrail_verdicts") or []
    fired = sorted({rule for v in verdicts for rule in v.get("fired", [])})
    ledger = result.get("tool_budget_remaining")

    return {
        "event": event,
        "injected": injected,
        "status": plan.get("status"),
        "relaxation": plan.get("relaxation_used"),
        "cost": plan.get("total_cost", 0.0),
        "binding": plan.get("binding_constraint"),
        "fired": fired,
        "requires_approval": result.get("requires_approval"),
        "paused": paused,
        "tools_called": len(result.get("tools_called") or []),
        "budget_remaining": ledger,
        "erp_writes": len(result.get("erp_writes") or []),
        "replans": result.get("replan_count"),
        "excluded": (plan.get("verification_delta") or {}).get("suppliers_excluded"),
        "error": None,
    }


def compare(row: dict) -> tuple[bool, str]:
    ref = REFERENCE[row["event"]]
    if row.get("error"):
        return False, "run raised"
    ref_status, ref_relax, ref_cost, ref_fired, ref_binding = ref
    notes = []
    if row["status"] != ref_status:
        notes.append(f"status {row['status']} vs {ref_status}")
    if row["relaxation"] != ref_relax:
        notes.append(f"rung {row['relaxation']} vs {ref_relax}")
    if round(row["cost"] or 0.0, 2) != ref_cost:
        notes.append(f"cost {row['cost']:,.0f} vs {ref_cost:,.0f}")
    if row["fired"] != sorted(ref_fired):
        notes.append(f"guardrails {row['fired']} vs {sorted(ref_fired)}")
    if row["binding"] != ref_binding:
        notes.append(f"binding {row['binding']} vs {ref_binding}")
    return (not notes), "; ".join(notes)


def main() -> int:
    base_url, server = start_sandbox()
    world = HttpSandbox(base_url)
    rows = []
    try:
        for i, event in enumerate(EVENTS, start=1):
            rows.append(run_event(world, event, i))
    finally:
        server.should_exit = True

    print(f"{'event':<7}{'status':<12}{'rung':<12}{'cost':>12}  "
          f"{'guardrails':<10}{'binding':<20}{'tools':>6}  match")
    divergences = []
    for row in rows:
        if row.get("error"):
            print(f"{row['event']:<7}RAISED")
            divergences.append((row["event"], "run raised", row["error"]))
            continue
        ok, note = compare(row)
        print(f"{row['event']:<7}{str(row['status']):<12}{str(row['relaxation']):<12}"
              f"{row['cost']:>12,.0f}  {','.join(row['fired']) or '-':<10}"
              f"{str(row['binding'] or '-'):<20}{row['tools_called']:>6}  "
              f"{'yes' if ok else 'NO — ' + note}")
        if not ok:
            divergences.append((row["event"], note, None))

    print()
    if divergences:
        print(f"{len(divergences)} divergence(s) from the deterministic table:")
        for event, note, tb in divergences:
            print(f"  {event}: {note}")
            if tb:
                print("    " + tb.strip().splitlines()[-1])
    else:
        print("No divergence. The node path reproduces the deterministic table "
              "on all ten events.")

    Path(__file__).parent.joinpath("agent_run_matrix.json").write_text(
        json.dumps(rows, indent=2, default=str) + "\n")
    return 1 if divergences else 0


if __name__ == "__main__":
    sys.exit(main())
