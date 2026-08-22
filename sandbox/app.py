"""The simulated ERP sandbox — FastAPI over one SQLite file.

The §4.6 endpoints, localhost only, no auth, no async workers, no message
queue. Response shapes come straight from contracts/models.py and are never
adjusted for convenience.

PS §18: nothing here reaches outside the process. No mail library, no ERP
SDK, no payment library. Prove it with the §2.6 grep.

STATUS: everything except the chaos injector. POST /sim/inject and
/sim/inject/sequence arrive with sandbox/chaos.py; the persona replies queued
by POST /suppliers/{id}/message arrive with sandbox/supplier_sim.py.
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from contracts.constants import APPROVAL_THRESHOLD
from contracts.models import (
    ApprovalResult, Component, Message, ProductionOrder, PurchaseOrder, Quote,
    Supplier, TrackingRecord,
)
from sandbox import attribution, chaos, db
from trust import (TrustEvent, effective_reliability, record_incident,
                   trust_write)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    lifespan=lifespan,
    title="SCDA simulated ERP sandbox",
    description="Fully simulated procurement environment. PS §18: no real "
                "suppliers, no real ERP, no real email, no real payments.",
    version="0.1.0",
)



def _component(row) -> Component:
    """One row -> one contract model. JSON columns decoded here, not in the route."""
    return Component(
        component_id=row["component_id"],
        name=row["name"],
        current_stock=row["current_stock"],
        usable_stock=row["usable_stock"],
        daily_usage=row["daily_usage"],
        safety_stock=row["safety_stock"],
        warehouse=row["warehouse"],
        last_updated=row["last_updated"],
        required_certifications=json.loads(row["required_certifications"]),
        min_quality=row["min_quality"],
    )


@app.get("/inventory", response_model=list[Component])
def get_inventory() -> list[Component]:
    """T-01. Every component. usable_stock is the field that matters."""
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM components ORDER BY component_id").fetchall()
    return [_component(r) for r in rows]


@app.get("/inventory/{component_id}", response_model=Component)
def get_component(component_id: str) -> Component:
    """T-01 by id. 404 rather than an empty list, so a typo is loud."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT * FROM components WHERE component_id = ?",
            (component_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown component: {component_id}")
    return _component(row)


# ---- row -> contract model ---------------------------------------------

def _supplier(row) -> Supplier:
    return Supplier(
        supplier_id=row["supplier_id"], supplier_name=row["supplier_name"],
        component_id=row["component_id"], unit_price=row["unit_price"],
        lead_time_days=row["lead_time_days"],
        available_quantity=row["available_quantity"],
        quality_score=row["quality_score"],
        reliability_score=row["reliability_score"],
        min_order_quantity=row["min_order_quantity"],
        certifications=json.loads(row["certifications"]),
    )


def _purchase_order(row) -> PurchaseOrder:
    return PurchaseOrder(**{k: row[k] for k in (
        "po_id", "component_id", "supplier_id", "quantity", "expected_delivery",
        "status", "unit_price", "total_value", "approval_required_above")})


def _production_order(row) -> ProductionOrder:
    return ProductionOrder(**{k: row[k] for k in (
        "production_order_id", "product", "required_component", "units_planned",
        "component_required_per_unit", "deadline", "priority", "max_delay_days")})


def _message(row) -> Message:
    return Message(**{k: row[k] for k in (
        "message_id", "sender", "recipient", "subject", "body", "related_po_id",
        "ts")})


# ---- request bodies ----------------------------------------------------
# Response shapes are frozen in contracts/; request shapes are the sandbox's
# own business and live here.

class SupplierView(Supplier):
    """Supplier, plus what the trust ledger has learned about it.

    A subclass, not an edit: every field of the frozen Supplier is present
    with the same name and the same meaning, and effective_reliability is
    added alongside reliability_score rather than replacing it. A caller sees
    both the catalog's opinion and ours, and can say which is which.

    contracts/models.py is not touched. HttpSandbox.get_suppliers still
    returns plain Supplier objects so the frozen SandboxClient protocol and
    stub/http parity both hold; get_suppliers_with_trust returns this.
    """

    effective_reliability: float


class MessageRequest(BaseModel):
    subject: str
    body: str
    trust_event: TrustEvent | None = None


class RfqRequest(BaseModel):
    component_id: str
    quantity: int
    needed_by_days: int
    supplier_ids: list[str]


class ApprovalRequest(BaseModel):
    action: str
    estimated_cost: float


class ErpRequest(BaseModel):
    action: str
    payload: dict


# ---- T-02 purchase orders ----------------------------------------------

@app.get("/purchase-orders", response_model=list[PurchaseOrder])
def get_purchase_orders() -> list[PurchaseOrder]:
    with db.session() as conn:
        rows = conn.execute("SELECT * FROM purchase_orders ORDER BY po_id").fetchall()
    return [_purchase_order(r) for r in rows]


@app.get("/purchase-orders/{po_id}", response_model=PurchaseOrder)
def get_purchase_order(po_id: str) -> PurchaseOrder:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM purchase_orders WHERE po_id = ?",
                           (po_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown purchase order: {po_id}")
    return _purchase_order(row)


# ---- T-03 supplier catalog ---------------------------------------------

@app.get("/suppliers", response_model=list[SupplierView])
def get_suppliers(component_id: str = Query(..., description="required")) -> list[SupplierView]:
    """§4.6: the query param is required. A catalog read without a component
    is a bug in the caller, not a request for every supplier we have.

    Each row carries effective_reliability alongside the catalog's
    reliability_score. Nothing existing is renamed or removed.
    """
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM suppliers WHERE component_id = ? ORDER BY supplier_id",
            (component_id,)).fetchall()
    return [
        SupplierView(**_supplier(r).model_dump(),
                     effective_reliability=effective_reliability(
                         r["supplier_id"], r["reliability_score"]))
        for r in rows
    ]


# ---- T-04 production schedule ------------------------------------------

@app.get("/production-schedule", response_model=list[ProductionOrder])
def get_production_schedule() -> list[ProductionOrder]:
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM production_orders ORDER BY deadline, production_order_id"
        ).fetchall()
    return [_production_order(r) for r in rows]


# ---- T-10 tracking, the ground truth against supplier claims ------------

@app.get("/tracking/{po_id}", response_model=TrackingRecord)
def get_tracking(po_id: str) -> TrackingRecord:
    with db.session() as conn:
        row = conn.execute("SELECT * FROM tracking WHERE po_id = ?",
                           (po_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"no tracking record: {po_id}")
    _record_contradiction(po_id, row)
    return TrackingRecord(po_id=row["po_id"], supplier_claim=row["supplier_claim"],
                          tracking_status=row["tracking_status"],
                          last_movement=row["last_movement"])


def _record_contradiction(po_id: str, row) -> None:
    """Log a discrepancy between the claim and the evidence, and attribute it.

    Two separate things happen here, and keeping them separate is the point:

      * An incident is recorded, always. A discrepancy is a fact about a
        shipment and gets logged whether or not anyone turns out to be at
        fault. It drops that PO's shipment confidence.
      * Reputation moves only when attribution comes back SUPPLIER. A label
        printed with no pickup scan is consistent with a supplier that never
        handed the goods over *and* with a courier that never came, so it is
        UNATTRIBUTED and nobody's reputation moves on it.

    record_incident is idempotent per (po_id, observed), so re-reading
    tracking does not compound either axis.
    """
    if not attribution.has_discrepancy(row):
        return
    with db.session() as conn:
        supplier = conn.execute(
            "SELECT supplier_id FROM purchase_orders WHERE po_id = ?",
            (po_id,)).fetchone()
    if supplier is None:
        return

    verdict, basis, observed, expected = attribution.classify(row, db.sim_now())
    record_incident(po_id=po_id, supplier_id=supplier["supplier_id"],
                    observed=observed, expected=expected,
                    attribution=verdict, attribution_basis=basis)


# ---- T-05 simulated inbox ----------------------------------------------

@app.get("/inbox", response_model=list[Message])
def get_inbox(since: datetime | None = None) -> list[Message]:
    """A queued persona reply stays invisible until its visible_at tick, so a
    follow-up and its answer can never be read in the same call."""
    now = db.sim_now().isoformat()
    sql = "SELECT * FROM messages WHERE visible_at <= ?"
    args: list = [now]
    if since is not None:
        sql += " AND ts > ?"
        args.append(since.isoformat())
    with db.session() as conn:
        rows = conn.execute(sql + " ORDER BY ts, message_id", args).fetchall()
    return [_message(r) for r in rows]


# ---- T-06 supplier communication ---------------------------------------

@app.post("/suppliers/{supplier_id}/message", response_model=Message)
def send_message(supplier_id: str, req: MessageRequest) -> Message:
    """Record the outbound message and queue the persona's reply.

    Goes to a SQLite table. There is no SMTP client, no IMAP client and no
    mail library anywhere in this repo (PS §18).
    """
    with db.session() as conn:
        known = conn.execute("SELECT 1 FROM suppliers WHERE supplier_id = ? LIMIT 1",
                             (supplier_id,)).fetchone()
        if known is None:
            raise HTTPException(status_code=404, detail=f"unknown supplier: {supplier_id}")
        now = db.sim_now()
        sent = Message(
            message_id=_next_message_id(conn),
            sender="ops@example.com",
            recipient=_supplier_address(supplier_id),
            subject=req.subject, body=req.body,
            related_po_id=_related_po(conn, supplier_id), ts=now,
        )
        conn.execute(
            "INSERT INTO messages (message_id, sender, recipient, subject, body,"
            " related_po_id, ts, visible_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sent.message_id, sent.sender, sent.recipient, sent.subject, sent.body,
             sent.related_po_id, now.isoformat(), now.isoformat()))
    _queue_reply(supplier_id, req.body)
    if req.trust_event is not None:
        # The caller observed something and says so explicitly. The sandbox
        # does not infer trust events from message traffic: the ledger is the
        # agent's memory, and a world that writes it behind the agent's back
        # makes the audit trail unexplainable.
        trust_write(supplier_id, req.trust_event)
    _record_reply_delay(supplier_id)
    return sent


def _record_reply_delay(supplier_id: str) -> None:
    """A reply that announces a delay is a late event, recorded once per reply."""
    with db.session() as conn:
        row = conn.execute(
            "SELECT message_id, body FROM messages WHERE sender = ?"
            " AND persona_reply = 1 ORDER BY message_id DESC LIMIT 1",
            (f"{supplier_id.lower().replace('-', '')}@example.com",)).fetchone()
        if row is None or "delay" not in row["body"].lower():
            return
        marker = f"trust_recorded:late:{row['message_id']}"
        if conn.execute("SELECT 1 FROM sim_flags WHERE key = ?",
                        (marker,)).fetchone():
            return
        conn.execute("INSERT INTO sim_flags (key, value) VALUES (?, '1')", (marker,))
    trust_write(supplier_id, "late")


def _queue_reply(supplier_id: str, follow_up_body: str) -> None:
    """Persona replies land with sandbox/supplier_sim.py (§4.8).

    Wired as a lookup rather than an import so this module stays importable
    while that file does not exist yet.
    """
    try:
        from sandbox import supplier_sim
    except ImportError:
        return
    supplier_sim.queue_reply(supplier_id, follow_up_body)


def _supplier_address(supplier_id: str) -> str:
    return f"{supplier_id.lower().replace('-', '')}@example.com"


def _related_po(conn, supplier_id: str) -> str | None:
    row = conn.execute(
        "SELECT po_id FROM purchase_orders WHERE supplier_id = ?"
        " ORDER BY po_id LIMIT 1", (supplier_id,)).fetchone()
    return row["po_id"] if row else None


def _next_message_id(conn) -> str:
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return f"MSG-{n + 1:04d}"


# ---- T-07 RFQ ----------------------------------------------------------

@app.post("/rfq", response_model=list[Quote])
def request_rfq(req: RfqRequest) -> list[Quote]:
    """Issued quotes are persisted so quote_valid_hours can actually expire
    them (G8). Recomputing a fresh quote on every read makes G8 untestable."""
    # Issuing quotes takes time, and that time is what a quote expires
    # against. Without the tick, quote_valid_hours could only ever run down
    # through message traffic, and an RFQ-only flow would see quotes that
    # never age.
    now = db.advance_clock(db.SIM_TICK)
    quotes: list[Quote] = []
    with db.session() as conn:
        for supplier_id in req.supplier_ids:
            row = conn.execute(
                "SELECT * FROM suppliers WHERE supplier_id = ? AND component_id = ?",
                (supplier_id, req.component_id)).fetchone()
            if row is None:
                continue
            withdrawn = conn.execute(
                "SELECT 1 FROM sim_flags WHERE key = ?",
                (f"expedite_withdrawn:{req.component_id}",)).fetchone() is not None
            q = Quote(
                supplier_id=supplier_id, component_id=req.component_id,
                quantity_available=min(req.quantity, row["available_quantity"]),
                unit_price=row["unit_price"], delivery_days=row["lead_time_days"],
                expedite_available=row["lead_time_days"] > 3 and not withdrawn,
                expedite_fee=8000.0, quote_valid_hours=6, issued_at=now,
            )
            conn.execute(
                "INSERT INTO quotes (supplier_id, component_id, quantity_available,"
                " unit_price, delivery_days, expedite_available, expedite_fee,"
                " quote_valid_hours, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (q.supplier_id, q.component_id, q.quantity_available, q.unit_price,
                 q.delivery_days, int(q.expedite_available), q.expedite_fee,
                 q.quote_valid_hours, now.isoformat()))
            quotes.append(q)
    return quotes


# ---- T-08 approval -----------------------------------------------------

@app.post("/approval/check", response_model=ApprovalResult)
def check_approval(req: ApprovalRequest) -> ApprovalResult:
    """PS §5.8, exactly. Strictly greater-than: a plan costing precisely
    150,000 executes autonomously."""
    approval_required = req.estimated_cost > APPROVAL_THRESHOLD
    return ApprovalResult(
        action=req.action, estimated_cost=req.estimated_cost,
        approval_required=approval_required,
        approval_reason=(
            f"Cost exceeds autonomous purchase threshold of {APPROVAL_THRESHOLD}"
            if approval_required else None),
    )


# ---- T-09 ERP writes ---------------------------------------------------

ERP_ACTIONS = (                                   # PS §5.9 — these six, no others
    "mark_po_delayed", "create_alternate_po", "attach_supplier_note",
    "update_production_risk", "record_escalation", "store_recovery_plan",
)


@app.post("/erp/update")
def erp_update(req: ErpRequest) -> dict:
    """Writes to a local SQLite file. No ERP SDK, no external API client.

    Every accepted write appends to erp_log with its full payload and a
    timestamp, so the demo can prove the writes landed.
    """
    if req.action not in ERP_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action: {req.action}. "
                   f"PS §5.9 permits only: {', '.join(ERP_ACTIONS)}")
    now = db.sim_now()
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM erp_log").fetchone()[0]
        record_id = f"ERP-{n + 1:04d}"
        conn.execute(
            "INSERT INTO erp_log (record_id, action, payload, ts) VALUES (?, ?, ?, ?)",
            (record_id, req.action, json.dumps(req.payload), now.isoformat()))
    return {"status": "ok", "message": f"{req.action} recorded",
            "record_id": record_id}


# ---- simulation control ------------------------------------------------

@app.get("/sim/clock")
def sim_clock() -> dict:
    return {"now": db.sim_now().isoformat()}


@app.post("/sim/reset")
def sim_reset() -> dict:
    """Reseed from scratch. What a rehearsal run calls between takes."""
    db.init_db(reset=True)
    return {"status": "ok", "now": db.sim_now().isoformat()}


app.include_router(chaos.router)
