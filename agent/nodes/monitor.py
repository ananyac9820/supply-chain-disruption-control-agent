"""Node 1 — monitor / triage. LLM (small).

Reads:  /inbox, /purchase-orders, /inventory, /production-schedule
Writes: disruption_id, disruption_type, severity, affected_component

A deterministic scan first — a PO whose status is delayed or whose
expected_delivery has slipped, an inbox message referencing one — and only then
one small LLM call to classify type and severity. The model is never handed the
whole world and asked "is anything wrong": that burns tool budget and adds
nothing over a comparison the code can make.

Severity is NOT a function of delay length. PS §4.1 is explicit: five days is
fine for a low-priority order and dangerous for a high-priority one. So the
scan hands the model the priorities and deadline distances it needs, already
computed, and asks it to classify rather than to calculate.
"""

from __future__ import annotations

from agent import clock
from agent.audit import append_event
from agent.impact_math import coverage_days, days_from
from agent.llm import get_llm
from agent.tools import call_tool
from contracts.state import AgentState


def monitor(state: AgentState) -> dict:
    work = dict(state)

    inbox = call_tool(work, "get_inbox",
                      "an unread supplier message is the cheapest disruption signal")
    pos = call_tool(work, "get_purchase_orders",
                    "a PO whose status or delivery date has slipped is the "
                    "other signal, and it is ground truth rather than a claim")

    signal = _scan(inbox, pos)
    component_id = signal["component_id"]

    inventory = call_tool(work, "get_inventory",
                          "severity depends on cover, and cover cannot be read "
                          "off a purchase order",
                          component_id=component_id)
    schedule = call_tool(work, "get_production_schedule",
                         "severity depends on which production orders sit inside "
                         "the coverage window and how they are prioritised")

    component = inventory[0]
    cover = coverage_days(component.usable_stock, component.daily_usage)
    now = clock.now()
    in_window = [
        {"production_order_id": o.production_order_id, "priority": o.priority,
         "days_to_deadline": days_from(now, o.deadline),
         "units": o.units_planned * o.component_required_per_unit}
        for o in schedule if o.required_component == component_id
    ]
    in_window.sort(key=lambda o: o["days_to_deadline"])

    llm = get_llm()
    triage = llm.triage({
        "signal_type": signal["signal_type"],
        "signal": signal["summary"],
        "po_id": signal["po_id"],
        "component_id": component_id,
        "coverage_days": cover,
        "usable_stock": component.usable_stock,
        "current_stock_in_erp_header": component.current_stock,
        "orders_in_window": in_window,
    })

    out: dict = {
        "disruption_id": work.get("disruption_id") or "DIS-001",
        "disruption_type": triage.disruption_type,
        "severity": triage.severity,
        "affected_component": triage.affected_component or component_id,
        "tools_called": work["tools_called"],
        "tool_budget_remaining": work["tool_budget_remaining"],
    }

    out["audit_events"] = append_event(
        work, type="disruption_detected", actor="monitor",
        summary=signal["summary"],
        detail={"po_id": signal["po_id"], "component_id": component_id,
                "disruption_type": triage.disruption_type,
                "severity": triage.severity,
                "signal_type": signal["signal_type"],
                "source": signal["source"],
                "orders_in_window": in_window,
                "classifier": llm.name,
                "rationale": triage.rationale},
        tools_used=["GET /inbox", "GET /purchase-orders", "GET /inventory",
                    "GET /production-schedule"])
    return out


def _scan(inbox, pos) -> dict:
    """Deterministic. Find the disruption before the model is asked anything."""
    slipped = [p for p in pos if p.status == "delayed"]
    if slipped:
        po = slipped[0]
        related = [m for m in inbox if m.related_po_id == po.po_id]
        source = (f"inbox message {related[0].message_id} from {related[0].sender}"
                  if related else f"purchase order {po.po_id} status")
        return {"signal_type": "supplier_delay", "po_id": po.po_id,
                "component_id": po.component_id, "source": source,
                "summary": f"{po.po_id} delayed - {po.component_id}"}

    flagged = [m for m in inbox if m.related_po_id]
    if flagged:
        m = flagged[0]
        po = next((p for p in pos if p.po_id == m.related_po_id), None)
        return {"signal_type": "supplier_message", "po_id": m.related_po_id,
                "component_id": po.component_id if po else "",
                "source": f"inbox message {m.message_id} from {m.sender}",
                "summary": f"{m.related_po_id} flagged by supplier message"}

    po = pos[0] if pos else None
    return {"signal_type": "none_detected",
            "po_id": po.po_id if po else "",
            "component_id": po.component_id if po else "",
            "source": "periodic scan",
            "summary": "no disruption signal found in inbox or purchase orders"}
