"""In-process fake sandbox — Track A §3.4 / master plan §4.3.

This is the piece that buys independence: Track B imports StubSandbox and
runs end-to-end from hour 2, never blocked on Track A's real sandbox.
Track A's HttpSandbox (sandbox/client.py, built later) must expose the same
methods and return the same shapes, and both must pass tests/contract/.

The merge at hour 12 is then one line:
    SANDBOX = StubSandbox()  ->  SANDBOX = HttpSandbox("http://localhost:8000")

Records here are the COMP-104 scenario from Track A §4.7, hardcoded. The
five-supplier catalog is the project's test fixture: a naive cheapest-first
or fastest-first agent picks SUP-18 and fails, because SUP-18 is uncertified.

FROZEN AT HOUR 1.5.
"""

from datetime import datetime, date, timedelta
from typing import Protocol

from .models import (
    Component, Supplier, PurchaseOrder, ProductionOrder, Quote, Message,
    TrackingRecord, ApprovalResult,
)
from .constants import APPROVAL_THRESHOLD


class SandboxClient(Protocol):
    def get_inventory(self, component_id: str | None = None) -> list[Component]: ...
    def get_purchase_orders(self, po_id: str | None = None) -> list[PurchaseOrder]: ...
    def get_suppliers(self, component_id: str) -> list[Supplier]: ...
    def get_production_schedule(self) -> list[ProductionOrder]: ...
    def get_inbox(self, since: datetime | None = None) -> list[Message]: ...
    def get_tracking(self, po_id: str) -> TrackingRecord: ...
    def send_message(self, supplier_id: str, subject: str, body: str) -> Message: ...
    def request_rfq(self, component_id: str, quantity: int, needed_by_days: int,
                    supplier_ids: list[str]) -> list[Quote]: ...
    def check_approval(self, action: str, estimated_cost: float) -> ApprovalResult: ...
    def erp_update(self, action: str, payload: dict) -> dict: ...


# The stub's simulated clock. Fixed so canned records are deterministic.
NOW = datetime(2026, 9, 2, 10, 0, 0)

ERP_ACTIONS = {                                   # PS §5.9 — the only six
    "mark_po_delayed", "create_alternate_po", "attach_supplier_note",
    "update_production_risk", "record_escalation", "store_recovery_plan",
}

# Track A §4.7 — engineered, not sampled. Do not randomise these rows.
_SUPPLIERS = [
    #  id      name              price lead avail qual  rel   moq  certs
    ("SUP-21", "Meridian Components", 118, 6, 1000, 0.91, 0.72, 200,
     ["ISO-9001", "Automotive-Grade"]),   # incumbent; lies about dispatch (H-08)
    ("SUP-42", "Kestrel Electronics", 132, 4, 700, 0.94, 0.81, 300,
     ["ISO-9001", "Automotive-Grade"]),   # the correct primary answer
    ("SUP-37", "Ardent Semiconductor", 141, 6, 400, 0.96, 0.88, 100,
     ["ISO-9001", "Automotive-Grade"]),   # reliable but short (H-04)
    ("SUP-18", "Novabyte Trading", 104, 3, 900, 0.79, 0.65, 250,
     ["ISO-9001"]),                       # cheapest AND fastest, uncertified (H-03, H-05)
    ("SUP-55", "Pallas Industrial", 126, 5, 350, 0.92, 0.55, 350,
     ["ISO-9001", "Automotive-Grade"]),   # MOQ == availability, forces overbuy
]

_PERSONA = {                                      # Track A §4.8
    "SUP-37": "honest", "SUP-42": "honest",
    "SUP-55": "vague",
    "SUP-21": "contradictory",
}


class StubSandbox:
    """Canned COMP-104 world. No network, no database, no file I/O."""

    def __init__(self) -> None:
        self._messages: list[Message] = [
            # SUP-21's scripted first reply on PO-7712, verbatim from PS §5.5.
            Message(
                message_id="MSG-0001",
                sender="supplier21@example.com",
                recipient="ops@example.com",
                subject="Delay on PO-7712",
                body=("Due to transport issues, delivery may be delayed by 5-7 days.\n"
                      "We are trying to resolve this and will update soon."),
                related_po_id="PO-7712",
                ts=NOW - timedelta(hours=1),
            ),
        ]
        self._erp_log: list[dict] = []
        self._reply_count: dict[str, int] = {}
        self._seq = 1

    # ---- reads -------------------------------------------------------

    def get_inventory(self, component_id: str | None = None) -> list[Component]:
        inv = [Component(
            component_id="COMP-104",
            name="Motor Driver IC",
            current_stock=420,
            usable_stock=390,            # coverage = 390 / 90 = 4.33 days
            daily_usage=90,
            safety_stock=150,
            warehouse="Pune-Plant-1",
            last_updated=NOW - timedelta(hours=6),
            required_certifications=["ISO-9001", "Automotive-Grade"],
            min_quality=0.90,
        )]
        if component_id:
            return [c for c in inv if c.component_id == component_id]
        return inv

    def get_purchase_orders(self, po_id: str | None = None) -> list[PurchaseOrder]:
        pos = [PurchaseOrder(
            po_id="PO-7712",
            component_id="COMP-104",
            supplier_id="SUP-21",
            quantity=600,
            expected_delivery=date(2026, 9, 3),
            status="delayed",
            unit_price=118.0,
            total_value=70800.0,
            approval_required_above=float(APPROVAL_THRESHOLD),
        )]
        if po_id:
            return [p for p in pos if p.po_id == po_id]
        return pos

    def get_suppliers(self, component_id: str) -> list[Supplier]:
        if component_id != "COMP-104":
            return []
        return [
            Supplier(
                supplier_id=sid, supplier_name=name, component_id="COMP-104",
                unit_price=float(price), lead_time_days=lead,
                available_quantity=avail, quality_score=qual,
                reliability_score=rel, min_order_quantity=moq,
                certifications=list(certs),
            )
            for sid, name, price, lead, avail, qual, rel, moq, certs in _SUPPLIERS
        ]

    def get_production_schedule(self) -> list[ProductionOrder]:
        return [
            ProductionOrder(
                production_order_id="PROD-882", product="Traction Controller",
                required_component="COMP-104", units_planned=700,
                component_required_per_unit=1, deadline=date(2026, 9, 6),
                priority="high", max_delay_days=0,
            ),
            # PROD-914 exists and is delayable. That is what makes rung 2 of
            # the infeasibility ladder demonstrable. Do not omit it.
            ProductionOrder(
                production_order_id="PROD-914", product="Auxiliary Drive Unit",
                required_component="COMP-104", units_planned=700,
                component_required_per_unit=1, deadline=date(2026, 9, 8),
                priority="low", max_delay_days=5,
            ),
        ]

    def get_inbox(self, since: datetime | None = None) -> list[Message]:
        if since is None:
            return list(self._messages)
        return [m for m in self._messages if m.ts > since]

    def get_tracking(self, po_id: str) -> TrackingRecord:
        # Ground truth, and it disagrees with SUP-21's claim (A-3 / H-08).
        return TrackingRecord(
            po_id=po_id,
            supplier_claim="dispatched",
            tracking_status="label_created_no_pickup",
            last_movement=None,
        )

    # ---- writes ------------------------------------------------------

    def send_message(self, supplier_id: str, subject: str, body: str) -> Message:
        """Record the outbound message and queue a persona reply.

        The real persona logic lives in sandbox/supplier_sim.py (Track A
        §4.8). This is a shape-faithful minimum so Track B can exercise the
        challenge-and-follow-up path before the merge.
        """
        sent = Message(
            message_id=self._next_id("MSG"),
            sender="ops@example.com",
            recipient=f"{supplier_id.lower().replace('-', '')}@example.com",
            subject=subject, body=body, related_po_id="PO-7712", ts=NOW,
        )
        self._messages.append(sent)
        self._messages.append(self._reply(supplier_id, body))
        return sent

    def request_rfq(self, component_id: str, quantity: int, needed_by_days: int,
                    supplier_ids: list[str]) -> list[Quote]:
        catalog = {s.supplier_id: s for s in self.get_suppliers(component_id)}
        return [
            Quote(
                supplier_id=sid, component_id=component_id,
                quantity_available=min(quantity, catalog[sid].available_quantity),
                unit_price=catalog[sid].unit_price,
                delivery_days=catalog[sid].lead_time_days,
                expedite_available=catalog[sid].lead_time_days > 3,
                expedite_fee=8000.0,
                quote_valid_hours=6,
                issued_at=NOW,
            )
            for sid in supplier_ids if sid in catalog
        ]

    def check_approval(self, action: str, estimated_cost: float) -> ApprovalResult:
        required = estimated_cost > APPROVAL_THRESHOLD      # PS §5.8, exactly
        return ApprovalResult(
            action=action, estimated_cost=estimated_cost,
            approval_required=required,
            approval_reason=(
                f"Cost exceeds autonomous purchase threshold of {APPROVAL_THRESHOLD}"
                if required else None
            ),
        )

    def erp_update(self, action: str, payload: dict) -> dict:
        if action not in ERP_ACTIONS:                        # PS §5.9
            return {"status": "rejected", "message": f"unknown action: {action}",
                    "record_id": None}
        record_id = self._next_id("ERP")
        self._erp_log.append({"record_id": record_id, "action": action,
                              "payload": payload, "ts": NOW})
        return {"status": "ok", "message": f"{action} recorded",
                "record_id": record_id}

    # ---- internals ---------------------------------------------------

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq:04d}"

    def _reply(self, supplier_id: str, follow_up_body: str) -> Message:
        persona = _PERSONA.get(supplier_id, "honest")
        n = self._reply_count.get(supplier_id, 0) + 1
        self._reply_count[supplier_id] = n
        specific = ("?" in follow_up_body
                    and ("date" in follow_up_body.lower()
                         or "quantity" in follow_up_body.lower()))

        if persona == "contradictory":
            body = ("Your order has been dispatched from our facility today. "
                    "Tracking will update shortly.")
        elif persona == "vague" and not (n > 1 and specific):
            body = "We are looking into this and will update you soon."
        else:
            body = ("Confirmed: 400 units, dispatch on 2026-09-04, "
                    "delivery by 2026-09-08.")

        return Message(
            message_id=self._next_id("MSG"),
            sender=f"{supplier_id.lower().replace('-', '')}@example.com",
            recipient="ops@example.com",
            subject=f"Re: PO-7712 / {supplier_id}",
            body=body, related_po_id="PO-7712", ts=NOW,
        )
