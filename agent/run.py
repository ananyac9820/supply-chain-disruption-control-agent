"""Entry point for a Track B run against the stub sandbox.

    python -m agent.run

Hour 12 merge is one line below: StubSandbox() -> HttpSandbox("http://localhost:8000").
"""

from __future__ import annotations

import argparse
import json

from langgraph.types import Command

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
    args = ap.parse_args()

    graph = build_graph()
    config = {"configurable": {"thread_id": args.disruption_id}}

    print(f"Track A integrations: {status()}")
    print(f"sandbox: {type(SANDBOX).__name__}\n")

    result = graph.invoke(initial_state(args.disruption_id), config)

    if "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        print("PAUSE  interrupt() fired, state checkpointed")
        print(json.dumps(payload, indent=2, default=str))
        print(f"\nRESUME Command(resume={{'decision': '{args.decision}'}})\n")
        result = graph.invoke(Command(resume={"decision": args.decision}), config)

    final = graph.get_state(config).values
    print(json.dumps(final, indent=2, default=str))


if __name__ == "__main__":
    main()
