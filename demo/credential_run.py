#!/usr/bin/env python3
"""Run the agent three times against the real model and report what varies.

    export ANTHROPIC_API_KEY=sk-...
    python demo/credential_run.py

This is the one axis the ten-event matrix cannot reach. RuleBasedLLM makes the
same choice every time by construction, so a matrix built on it says nothing
about whether the model's tool selection is stable, whether its necessity
strings are worth showing a judge, or whether its classifications agree with
the stand-in's.

Answers, in the order they matter:

  1. Does the final plan move between runs?  If yes, nothing else matters —
     a demo whose answer changes on the second run is not a demo.
  2. How much does the tool SEQUENCE vary?   Order and count, run to run.
  3. Does it stay inside the 15-call investigation budget?
  4. Do the classifications land where the stand-in put them?
  5. Do the necessity strings read like something a judge should see?
  6. Does severity key off production-order priority, not delay length?

Costs real tokens: three full agent runs, each with several structured calls.

    --runs N              how many runs (default 3)
    --allow-rule-based    self-test the harness with no credential; proves the
                          plumbing works but answers none of the questions
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import sys
import threading
import time
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.types import Command

from contracts.constants import TOOL_BUDGET_PER_DISRUPTION
from sandbox.client import HttpSandbox


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


def one_run(world: HttpSandbox, index: int) -> dict:
    from agent import clock, tools
    from agent.audit import close_log, open_log
    from agent.graph import build_graph, initial_state
    from agent.ledger import get_ledger, reset_ledger

    disruption_id = f"DIS-{index:03d}"
    world.sim_reset()
    tools.set_sandbox(world)
    clock.reset()
    reset_ledger(disruption_id)

    path = f"/tmp/credential-run-{index}.jsonl"
    open_log(disruption_id, path=path)
    graph = build_graph()
    config = {"configurable": {"thread_id": f"cred-{index}"}}

    started = time.monotonic()
    with redirect_stdout(io.StringIO()):
        result = graph.invoke(initial_state(disruption_id), config)
        if "__interrupt__" in result:
            result = graph.invoke(Command(resume={"decision": "approve"}), config)
    elapsed = time.monotonic() - started
    close_log(disruption_id)

    events = [json.loads(line) for line in open(path)]
    calls = [e for e in events if e["type"] == "tool_call"]
    charged = [e for e in calls if "cache hit" not in e["summary"]]
    verifications = [e for e in events if e["type"] == "verification"]
    plan = result.get("plan") or {}

    return {
        "run": index,
        "seconds": round(elapsed, 1),
        "sequence": [e["summary"].split("(")[0].strip() for e in charged],
        "cached": len(calls) - len(charged),
        "budget_remaining": get_ledger(disruption_id).remaining,
        "necessities": [e.get("necessity") for e in charged if e.get("necessity")],
        "classifications": [v["summary"] for v in verifications],
        "severity": result.get("severity"),
        "disruption_type": result.get("disruption_type"),
        "plan": {
            "status": plan.get("status"),
            "relaxation_used": plan.get("relaxation_used"),
            "total_cost": plan.get("total_cost"),
            "allocations": sorted((a["supplier_id"], a["units"])
                                  for a in plan.get("allocations", [])),
            "reschedules": sorted((r["production_order_id"], r["delay_days"])
                                  for r in plan.get("reschedules", [])),
        },
        "guardrails": sorted({r for v in (result.get("guardrail_verdicts") or [])
                              for r in v.get("fired", [])}),
        "requires_approval": result.get("requires_approval"),
    }


def report(runs: list[dict], live: bool) -> int:
    rule = "─" * 78
    plans = [json.dumps(r["plan"], sort_keys=True) for r in runs]
    stable = len(set(plans)) == 1

    print(f"\n{rule}\n1. DOES THE PLAN MOVE BETWEEN RUNS?\n{rule}")
    if stable:
        p = runs[0]["plan"]
        print(f"   NO — identical across {len(runs)} runs.")
        print(f"   {p['status']} · {p['relaxation_used']} · {p['total_cost']:,.2f}")
        print(f"   {p['allocations']}  reschedules {p['reschedules']}")
    else:
        print(f"   YES — the plan differs across runs. This is the finding.")
        for r in runs:
            print(f"   run {r['run']}: {json.dumps(r['plan'], sort_keys=True)}")

    print(f"\n{rule}\n2. TOOL SEQUENCE VARIANCE\n{rule}")
    sequences = [tuple(r["sequence"]) for r in runs]
    print(f"   distinct sequences: {len(set(sequences))} of {len(runs)}")
    for r in runs:
        print(f"   run {r['run']}: {len(r['sequence'])} charged calls, "
              f"{r['cached']} cached, {r['seconds']}s")
        print(f"      {' → '.join(r['sequence'])}")
    if len(set(sequences)) > 1:
        counts = [Counter(s) for s in sequences]
        every = set().union(*[set(c) for c in counts])
        differing = [t for t in sorted(every)
                     if len({c.get(t, 0) for c in counts}) > 1]
        print(f"   tools whose call count varies: {differing or 'none'}")

    print(f"\n{rule}\n3. INVESTIGATION BUDGET ({TOOL_BUDGET_PER_DISRUPTION} calls)\n{rule}")
    for r in runs:
        used = TOOL_BUDGET_PER_DISRUPTION - r["budget_remaining"]
        flag = "OK" if r["budget_remaining"] >= 0 and used <= TOOL_BUDGET_PER_DISRUPTION else "OVER"
        print(f"   run {r['run']}: {used} used, {r['budget_remaining']} remaining  {flag}")

    print(f"\n{rule}\n4. CLASSIFICATIONS\n{rule}")
    for r in runs:
        print(f"   run {r['run']}:")
        for c in r["classifications"]:
            print(f"      {c}")
    print(f"\n   severity across runs: {[r['severity'] for r in runs]}")
    print(f"   disruption_type:      {[r['disruption_type'] for r in runs]}")

    print(f"\n{rule}\n5. NECESSITY STRINGS — would you show these to a judge?\n{rule}")
    for n in dict.fromkeys(n for r in runs for n in r["necessities"]):
        print(f"   · {n}")

    print(f"\n{rule}")
    if not live:
        print("SELF-TEST ONLY — RuleBasedLLM was used, so questions 1, 2, 4 and 5")
        print("are answered by a deterministic stand-in and mean nothing yet.")
        return 0
    print("PLAN STABLE" if stable else "PLAN NOT STABLE — report this first")
    return 0 if stable else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--allow-rule-based", action="store_true")
    args = ap.parse_args()

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key and not args.allow_rule_based:
        print("No ANTHROPIC_API_KEY in the environment.\n\n"
              "This harness exists to test the live model; running it against\n"
              "RuleBasedLLM would produce three identical runs and answer none\n"
              "of the questions it asks. Set the key and re-run:\n\n"
              "    export ANTHROPIC_API_KEY=sk-...\n"
              "    python demo/credential_run.py\n\n"
              "Or pass --allow-rule-based to self-test the harness plumbing.")
        return 2

    base_url, server = start_sandbox()
    world = HttpSandbox(base_url)
    try:
        runs = [one_run(world, i) for i in range(1, args.runs + 1)]
    finally:
        server.should_exit = True

    code = report(runs, live=has_key)
    Path(__file__).parent.joinpath("credential_run.json").write_text(
        json.dumps(runs, indent=2, default=str) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
