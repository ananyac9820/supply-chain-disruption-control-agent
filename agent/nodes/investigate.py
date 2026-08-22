"""Node 3 — investigate. LLM (the main reasoning loop).

Writes: tools_called, tool_budget_remaining, messages_sent, replies_received,
        claims, quotes

Real logic (hour 6): the LLM decides which tool to call next and must state
why. The necessity string goes into the tool ledger and then the audit trail,
so "why did it call tracking?" is answered from a recorded field rather than
by asking the model to rationalise afterwards. Decision policy (PS §4.3):

    stock risk unclear             -> inventory tools
    delivery status uncertain      -> supplier message, then tracking
    supply cannot meet demand      -> RFQ
    decision crosses budget limits -> approval check
    only after deciding            -> ERP update

Verification is a sub-step here, not a separate node. Every supplier claim is
tagged GROUNDED / CONTRADICTED / UNVERIFIABLE before it enters state["claims"],
and an UNVERIFIABLE certification claim is treated as absent, not present.
Replies are classified VAGUE or SPECIFIC; a VAGUE reply must not advance the
plan.

This node is also the re-entry point for a replan: node 6 routes back here
with broken_assumption set, and §4.5 says re-solve for the shortfall only —
already-confirmed units stay committed and verified quotes are preserved.

HOLLOW PASS: writes its six keys empty and burns no budget.
"""

from __future__ import annotations

from agent.audit import append_event
from contracts.constants import TOOL_BUDGET_PER_DISRUPTION
from contracts.state import AgentState


def investigate(state: AgentState) -> dict:
    replanning = bool(state.get("broken_assumption"))

    # STUB — hour 6 puts the tool-selection loop here, routed through
    # agent/tools.py so every call is metered and carries a necessity string.
    out: dict = {
        "tools_called": list(state.get("tools_called") or []),
        "tool_budget_remaining": state.get(
            "tool_budget_remaining", TOOL_BUDGET_PER_DISRUPTION
        ),
        "messages_sent": list(state.get("messages_sent") or []),
        "replies_received": list(state.get("replies_received") or []),
        "claims": list(state.get("claims") or []),
        # Preserved deliberately across a replan (§4.5) and across the node 5
        # pause — tests/agent asserts quotes survive interrupt()/resume.
        "quotes": list(state.get("quotes") or []),
    }

    out["audit_events"] = append_event(
        state,
        type="replan" if replanning else "tool_call",
        actor="investigate",
        summary=(
            f"STUB re-investigation after {state.get('broken_assumption')} broke "
            "(hollow node 3)" if replanning
            else "STUB investigation, no tools called (hollow node 3)"
        ),
        detail={"stub": True, "node": "investigate", "replanning": replanning},
        necessity="hollow pass: node 3 makes no tool call yet",
    )
    return out
