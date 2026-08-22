"""The chaos injector — POST /sim/inject fires H-01 … H-10 (§4.8).

Every event mutates real state and is verifiable with a read afterwards. No
event is a flag the agent is told about; it changes the world and the agent
finds out the same way it finds out about anything else.

This is the highest-value thing in the sandbox after the endpoints: it lets
the team rehearse against the organisers' hidden tests before the organisers
run them, and it is the live demo finale.
"""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sandbox import db

router = APIRouter()

DEFAULT_COMPONENT = "COMP-104"


class InjectRequest(BaseModel):
    event: str
    params: dict = {}


class SequenceStep(BaseModel):
    event: str
    params: dict = {}
    delay_minutes: int = 0


class SequenceRequest(BaseModel):
    steps: list[SequenceStep]


# ---- helpers -----------------------------------------------------------

def _queue_message(conn, sender: str, subject: str, body: str,
                   po_id: str | None) -> str:
    now = db.sim_now()
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    message_id = f"MSG-{n + 1:04d}"
    conn.execute(
        "INSERT INTO messages (message_id, sender, recipient, subject, body,"
        " related_po_id, ts, visible_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, sender, "ops@example.com", subject, body, po_id,
         now.isoformat(), now.isoformat()))
    return message_id


def _component(params: dict) -> str:
    return params.get("component_id", DEFAULT_COMPONENT)


def _require(row, what: str):
    if row is None:
        raise HTTPException(status_code=404, detail=f"nothing to disrupt: {what}")
    return row


# ---- the ten events ----------------------------------------------------

def h01_supplier_delays_after_confirming(conn, params: dict) -> dict:
    """Push a confirmed PO five days out and tell us about it after the fact."""
    po = _require(conn.execute(
        "SELECT * FROM purchase_orders WHERE component_id = ? ORDER BY po_id LIMIT 1",
        (_component(params),)).fetchone(), "purchase order")
    days = params.get("days", 5)
    new_date = (datetime.fromisoformat(po["expected_delivery"])
                + timedelta(days=days)).date().isoformat()
    conn.execute(
        "UPDATE purchase_orders SET expected_delivery = ?, status = 'delayed'"
        " WHERE po_id = ?", (new_date, po["po_id"]))
    _queue_message(
        conn, f"{po['supplier_id'].lower().replace('-', '')}@example.com",
        f"Delay on {po['po_id']}",
        f"Due to transport issues, delivery may be delayed by {days}-{days + 2} days. "
        "We are trying to resolve this and will update soon.",
        po["po_id"])
    return {"po_id": po["po_id"], "expected_delivery": new_date,
            "was": po["expected_delivery"]}


def h02_erp_overstates_stock(conn, params: dict) -> dict:
    """The header says 800. Only 390 is usable. Reason from usable_stock."""
    component_id = _component(params)
    current = params.get("current_stock", 800)
    usable = params.get("usable_stock", 390)
    row = _require(conn.execute(
        "SELECT current_stock, usable_stock FROM components WHERE component_id = ?",
        (component_id,)).fetchone(), component_id)
    conn.execute(
        "UPDATE components SET current_stock = ?, usable_stock = ? WHERE component_id = ?",
        (current, usable, component_id))
    return {"component_id": component_id, "current_stock": current,
            "usable_stock": usable,
            "was": {"current_stock": row["current_stock"],
                    "usable_stock": row["usable_stock"]}}


def h03_cheapest_fails_quality(conn, params: dict) -> dict:
    """Drop the cheapest still-eligible supplier below the quality floor.

    §4.8 names SUP-18 as the target. In the seeded catalog SUP-18 is already
    uncertified AND already under min_quality, so mutating it changes nothing
    the agent can observe — H-03 would report success and inject no
    disruption. The event therefore targets the cheapest supplier that
    currently passes both filters, unless params names one explicitly.
    """
    component_id = _component(params)
    comp = _require(conn.execute(
        "SELECT required_certifications, min_quality FROM components"
        " WHERE component_id = ?", (component_id,)).fetchone(), component_id)
    required = set(json.loads(comp["required_certifications"]))
    floor = comp["min_quality"]

    target_id = params.get("supplier_id")
    if target_id is None:
        rows = conn.execute(
            "SELECT * FROM suppliers WHERE component_id = ? ORDER BY unit_price",
            (component_id,)).fetchall()
        eligible = [r for r in rows
                    if required <= set(json.loads(r["certifications"]))
                    and r["quality_score"] >= floor]
        target_id = _require(eligible[0] if eligible else None,
                             "an eligible supplier")["supplier_id"]

    row = _require(conn.execute(
        "SELECT quality_score FROM suppliers WHERE supplier_id = ? AND component_id = ?",
        (target_id, component_id)).fetchone(), target_id)
    new_quality = round(floor - 0.05, 4)
    conn.execute(
        "UPDATE suppliers SET quality_score = ? WHERE supplier_id = ? AND component_id = ?",
        (new_quality, target_id, component_id))
    return {"supplier_id": target_id, "quality_score": new_quality,
            "was": row["quality_score"], "min_quality": floor}


def h04_reliable_supplier_short(conn, params: dict) -> dict:
    """The supplier you trust most cannot cover the gap alone."""
    supplier_id = params.get("supplier_id", "SUP-37")
    quantity = params.get("available_quantity", 200)
    row = _require(conn.execute(
        "SELECT available_quantity FROM suppliers WHERE supplier_id = ?",
        (supplier_id,)).fetchone(), supplier_id)
    conn.execute("UPDATE suppliers SET available_quantity = ? WHERE supplier_id = ?",
                 (quantity, supplier_id))
    return {"supplier_id": supplier_id, "available_quantity": quantity,
            "was": row["available_quantity"]}


def h05_low_reliability_fastest(conn, params: dict) -> dict:
    """The fastest option is also the one that lets you down."""
    supplier_id = params.get("supplier_id", "SUP-18")
    lead = params.get("lead_time_days", 2)
    reliability = params.get("reliability_score", 0.5)
    row = _require(conn.execute(
        "SELECT lead_time_days, reliability_score FROM suppliers WHERE supplier_id = ?",
        (supplier_id,)).fetchone(), supplier_id)
    conn.execute(
        "UPDATE suppliers SET lead_time_days = ?, reliability_score = ?"
        " WHERE supplier_id = ?", (lead, reliability, supplier_id))
    return {"supplier_id": supplier_id, "lead_time_days": lead,
            "reliability_score": reliability,
            "was": {"lead_time_days": row["lead_time_days"],
                    "reliability_score": row["reliability_score"]}}


def h06_demand_spike(conn, params: dict) -> dict:
    """A plan that was comfortable at 90/day may fail at 130/day."""
    component_id = _component(params)
    usage = params.get("daily_usage", 130)
    row = _require(conn.execute(
        "SELECT daily_usage FROM components WHERE component_id = ?",
        (component_id,)).fetchone(), component_id)
    conn.execute("UPDATE components SET daily_usage = ? WHERE component_id = ?",
                 (usage, component_id))
    return {"component_id": component_id, "daily_usage": usage,
            "was": row["daily_usage"]}


def h07_expedite_withdrawn(conn, params: dict) -> dict:
    """Expedite disappears from every future quote for this component.

    Recorded as a flag rather than a column edit because it applies to quotes
    not yet issued. /rfq reads it.
    """
    component_id = _component(params)
    conn.execute("INSERT OR REPLACE INTO sim_flags (key, value) VALUES (?, '1')",
                 (f"expedite_withdrawn:{component_id}",))
    conn.execute(
        "UPDATE quotes SET expedite_available = 0 WHERE component_id = ?",
        (component_id,))
    return {"component_id": component_id, "expedite_available": False}


def h08_false_dispatch_claim(conn, params: dict) -> dict:
    """Claims dispatch. Tracking says the label was printed and nothing moved."""
    po_id = params.get("po_id", "PO-7712")
    po = _require(conn.execute(
        "SELECT supplier_id FROM purchase_orders WHERE po_id = ?", (po_id,)).fetchone(),
        po_id)
    conn.execute(
        "INSERT INTO tracking (po_id, supplier_claim, tracking_status, last_movement)"
        " VALUES (?, 'dispatched', 'label_created_no_pickup', NULL)"
        " ON CONFLICT(po_id) DO UPDATE SET supplier_claim = 'dispatched',"
        " tracking_status = 'label_created_no_pickup', last_movement = NULL",
        (po_id,))
    message_id = _queue_message(
        conn, f"{po['supplier_id'].lower().replace('-', '')}@example.com",
        f"Dispatch confirmation for {po_id}",
        "Your order has been dispatched from our facility today. "
        "Tracking will update shortly.", po_id)
    return {"po_id": po_id, "tracking_status": "label_created_no_pickup",
            "message_id": message_id}


def h09_cost_exceeds_approval_limit(conn, params: dict) -> dict:
    """Raise the alternates until the only feasible plan needs a human."""
    component_id = _component(params)
    factor = params.get("factor", 1.4)
    incumbent = conn.execute(
        "SELECT supplier_id FROM purchase_orders WHERE component_id = ?"
        " ORDER BY po_id LIMIT 1", (component_id,)).fetchone()
    incumbent_id = incumbent["supplier_id"] if incumbent else ""
    before = {r["supplier_id"]: r["unit_price"] for r in conn.execute(
        "SELECT supplier_id, unit_price FROM suppliers WHERE component_id = ?"
        " AND supplier_id != ?", (component_id, incumbent_id)).fetchall()}
    conn.execute(
        "UPDATE suppliers SET unit_price = ROUND(unit_price * ?, 2)"
        " WHERE component_id = ? AND supplier_id != ?",
        (factor, component_id, incumbent_id))
    return {"component_id": component_id, "factor": factor,
            "incumbent_untouched": incumbent_id, "was": before}


def h10_priority_changes(conn, params: dict) -> dict:
    """The order you were going to delay is now the one you cannot."""
    order_id = params.get("production_order_id", "PROD-914")
    priority = params.get("priority", "high")
    max_delay = params.get("max_delay_days", 0)
    row = _require(conn.execute(
        "SELECT priority, max_delay_days FROM production_orders"
        " WHERE production_order_id = ?", (order_id,)).fetchone(), order_id)
    conn.execute(
        "UPDATE production_orders SET priority = ?, max_delay_days = ?"
        " WHERE production_order_id = ?", (priority, max_delay, order_id))
    return {"production_order_id": order_id, "priority": priority,
            "max_delay_days": max_delay,
            "was": {"priority": row["priority"],
                    "max_delay_days": row["max_delay_days"]}}


EVENTS = {
    "H-01": h01_supplier_delays_after_confirming,
    "H-02": h02_erp_overstates_stock,
    "H-03": h03_cheapest_fails_quality,
    "H-04": h04_reliable_supplier_short,
    "H-05": h05_low_reliability_fastest,
    "H-06": h06_demand_spike,
    "H-07": h07_expedite_withdrawn,
    "H-08": h08_false_dispatch_claim,
    "H-09": h09_cost_exceeds_approval_limit,
    "H-10": h10_priority_changes,
}


# ---- routes ------------------------------------------------------------

def _inject(event: str, params: dict) -> dict:
    handler = EVENTS.get(event)
    if handler is None:
        raise HTTPException(
            status_code=400,
            detail=f"unknown event: {event}. Known: {', '.join(sorted(EVENTS))}")
    now = db.sim_now()
    with db.session() as conn:
        detail = handler(conn, params)
        n = conn.execute("SELECT COUNT(*) FROM chaos_log").fetchone()[0]
        disruption_id = f"DIS-{n + 1:03d}"
        conn.execute(
            "INSERT INTO chaos_log (disruption_id, event, params, ts)"
            " VALUES (?, ?, ?, ?)",
            (disruption_id, event, json.dumps(params), now.isoformat()))
    return {"disruption_id": disruption_id, "event": event, "ts": now.isoformat(),
            "detail": detail}


@router.post("/sim/inject")
def inject(req: InjectRequest) -> dict:
    return _inject(req.event, req.params)


@router.post("/sim/inject/sequence")
def inject_sequence(req: SequenceRequest) -> list[dict]:
    """Fire a timed cascade so nobody has to type during the pitch."""
    results = []
    for step in req.steps:
        if step.delay_minutes:
            db.advance_clock(timedelta(minutes=step.delay_minutes))
        results.append(_inject(step.event, step.params))
    return results
