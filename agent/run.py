"""Entry point for a Track B run against the stub sandbox.

    python -m agent.run
    python -m agent.run --decision reject      answer the escalation differently
    python -m agent.run --trace                render the run's audit trail after it

Hour 12 merge is one line below: StubSandbox() -> HttpSandbox("http://localhost:8000").

Audit events are written to audit_logs/<disruption_id>.jsonl as the run goes,
not held in state, so a second terminal can watch it live:

    python -m output.cli audit_logs/DIS-001.jsonl --follow
"""

from __future__ import annotations

import argparse
import json

from langgraph.types import Command

from agent.audit import close_log, open_log
from agent.graph import build_graph, initial_state
from agent.integrations import status
from contracts.stub_sandbox import StubSandbox

SANDBOX = StubSandbox()          # -> HttpSandbox("http://localhost:8000") at hour 12


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--disruption-id", default="DIS-001")
    ap.add_argument("--decision", default="approve",
                    choices=["approve", "edit", "reject"],
                    help="what the coordinator answers if the run pauses")
    ap.add_argument("--audit-path", default=None,
                    help="where to write the audit JSONL")
    ap.add_argument("--trace", action="store_true",
                    help="render the audit trail through output/cli.py when the run ends")
    ap.add_argument("--state", action="store_true",
                    help="dump the final AgentState as JSON")
    args = ap.parse_args()

    log = open_log(args.disruption_id, path=args.audit_path)
    graph = build_graph()
    config = {"configurable": {"thread_id": args.disruption_id}}

    print(f"Track A integrations: {status()}")
    print(f"sandbox: {type(SANDBOX).__name__}")
    print(f"audit:   {log.path}\n")

    result = graph.invoke(initial_state(args.disruption_id), config)

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("PAUSE  interrupt() fired, state checkpointed")
        print(json.dumps(payload, indent=2, default=str))
        print(f"\nRESUME Command(resume={{'decision': '{args.decision}'}})\n")
        graph.invoke(Command(resume={"decision": args.decision}), config)

    final = graph.get_state(config).values
    print(f"run complete - {log.count} events -> {log.path}")
    close_log(args.disruption_id)

    if args.state:
        print(json.dumps(final, indent=2, default=str))

    if args.trace:
        from output.cli import TraceRenderer
        print()
        TraceRenderer(speed=0).render_file(log.path)


if __name__ == "__main__":
    main()
