"""Node 1 — monitor / triage. LLM (small).

Reads:  /inbox, /purchase-orders, /inventory, /production-schedule
Writes: disruption_id, disruption_type, severity, affected_component

Real logic (hour 6): a deterministic scan first — a PO whose expected_delivery
has slipped, a component whose coverage is below threshold, an unread inbox
message — and only then one small LLM call to classify type and severity.
Severity is not a function of delay length: five days is fine for a low
priority order and dangerous for a high priority one (PS §4.1), so it depends
on the affected production order's priority and deadline.

HOLLOW PASS: writes its four keys from the stub's canned disruption.
"""

from __future__ import annotations

from agent.audit import append_event
from contracts.state import AgentState


def monitor(state: AgentState) -> dict:
    disruption_id = state.get("disruption_id") or "DIS-001"

    # STUB — hour 6 replaces this with the deterministic scan + one LLM call.
    out: dict = {
        "disruption_id": disruption_id,
        "disruption_type": "supplier_delay",
        "severity": "high",
        "affected_component": "COMP-104",
    }

    out["audit_events"] = append_event(
        {**state, "disruption_id": disruption_id},
        type="disruption_detected",
        actor="monitor",
        summary="STUB disruption detected on COMP-104 (hollow node 1)",
        detail={"stub": True, "node": "monitor"},
    )
    return out
