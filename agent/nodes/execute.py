"""Node 6 — execute. LLM (writing only).

Writes: erp_writes, audit_events, broken_assumption

Real logic (hour 14): ERP writes via POST /erp/update using only the six
actions PS §5.9 permits, then the decision brief, then flush the audit events.
It also registers the assumptions this plan depends on (§4.4) — the watcher
re-checks them, and when one breaks the graph routes back into node 3 with
broken_assumption set and an assumption_break event fired BEFORE the LLM says
anything. That ordering is what makes Recovery deterministic rather than
hopeful.

HOLLOW PASS: writes its keys, registers no assumptions and breaks none, so
route_after_execute takes the terminal edge. Setting broken_assumption here
is what exercises the replan edge back into node 3.
"""

from __future__ import annotations

from agent.audit import append_event
from agent.integrations import status
from contracts.state import AgentState


def execute(state: AgentState) -> dict:
    human = state.get("human_response") or {}

    # STUB — hour 14 writes to /erp/update here, then renders the brief.
    out: dict = {
        "erp_writes": list(state.get("erp_writes") or []),
        # None -> terminal edge. The assumption watcher sets this to an
        # assumption id ("A3") to route back into node 3.
        "broken_assumption": None,
        "replan_count": int(state.get("replan_count", 0)),
    }

    events = append_event(
        state, type="erp_update", actor="executor",
        summary="STUB execution, no ERP writes performed (hollow node 6)",
        detail={"stub": True, "node": "execute", "decision": human.get("decision")},
    )
    out["audit_events"] = append_event(
        {**state, "audit_events": events},
        type="run_complete", actor="executor",
        summary="STUB run complete (hollow graph walked all six nodes)",
        detail={"stub": True, "node": "execute", "track_a_integrations": status()},
        remaining_risk="every node is still a stub; no real plan was executed",
    )
    return out
