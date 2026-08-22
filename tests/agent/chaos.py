"""A chaos double for Track B's tests.

Track A owns the real injector — sandbox/chaos.py behind POST /sim/inject — and
it arrives at the hour-12 merge. Until then Track B still has to prove the
seven §4.4 triggers fire, so this wraps StubSandbox and mutates what it returns.

It is a TEST DOUBLE, not a second implementation of the sandbox: it holds no
records of its own, it delegates every call, and it only edits the objects on
the way back. When the real injector lands these tests point at it instead and
the assertions do not change.

    sandbox = ChaosSandbox()
    agent.tools.set_sandbox(sandbox)
    sandbox.inject("H-07")          # expedite withdrawn
"""

from __future__ import annotations

from contracts.stub_sandbox import StubSandbox

EVENTS = {
    "H-01": "supplier delays again after confirming a revised date",
    "H-02": "ERP overstates stock; usable corrected downward",
    "H-03": "cheapest supplier fails the quality floor",
    "H-04": "high-reliability supplier has insufficient quantity",
    "H-05": "low-reliability supplier is the fastest",
    "H-06": "demand spike mid-run",
    "H-07": "expedite becomes unavailable",
    "H-08": "supplier claims dispatch, tracking contradicts",
    "H-09": "purchase exceeds the approval limit",
    "H-10": "production priority changes mid-simulation",
}


class ChaosSandbox:
    """Delegates to StubSandbox, then applies whatever has been injected."""

    def __init__(self, inner: StubSandbox | None = None) -> None:
        self._inner = inner or StubSandbox()
        self.injected: list[str] = []

    def inject(self, event: str) -> dict:
        if event not in EVENTS:
            raise ValueError(f"unknown chaos event {event}")
        self.injected.append(event)
        return {"ok": True, "event": event, "description": EVENTS[event]}

    # ---- mutated reads -----------------------------------------------

    def get_inventory(self, component_id=None):
        items = self._inner.get_inventory(component_id)
        for c in items:
            if "H-02" in self.injected:
                c.current_stock = 800          # the ERP header lies
                c.usable_stock = 240           # and the reality is worse
        return items

    def get_production_schedule(self):
        orders = self._inner.get_production_schedule()
        for o in orders:
            if "H-06" in self.injected and o.production_order_id == "PROD-882":
                o.units_planned += 150         # demand spike
            if "H-10" in self.injected and o.production_order_id == "PROD-914":
                o.priority = "high"            # priority change
                o.max_delay_days = 0
        return orders

    def get_suppliers(self, component_id):
        sups = self._inner.get_suppliers(component_id)
        for s in sups:
            if "H-03" in self.injected and s.supplier_id == "SUP-18":
                s.quality_score = 0.61
            if "H-04" in self.injected and s.supplier_id == "SUP-37":
                s.available_quantity = 120     # cannot honour the commitment
            if "H-05" in self.injected and s.supplier_id == "SUP-18":
                s.lead_time_days = 2
            if "H-09" in self.injected and s.supplier_id in ("SUP-37", "SUP-42",
                                                             "SUP-55"):
                s.unit_price = round(s.unit_price * 1.12, 2)
        return sups

    def request_rfq(self, component_id, quantity, needed_by_days, supplier_ids):
        quotes = self._inner.request_rfq(component_id, quantity, needed_by_days,
                                         supplier_ids)
        for q in quotes:
            if "H-07" in self.injected:
                q.expedite_available = False   # expedite withdrawn
                q.expedite_fee = 0.0
        return quotes

    # ---- straight delegation -----------------------------------------

    def get_purchase_orders(self, po_id=None):
        return self._inner.get_purchase_orders(po_id)

    def get_inbox(self, since=None):
        return self._inner.get_inbox(since)

    def get_tracking(self, po_id):
        return self._inner.get_tracking(po_id)

    def send_message(self, supplier_id, subject, body):
        return self._inner.send_message(supplier_id, subject, body)

    def check_approval(self, action, estimated_cost):
        return self._inner.check_approval(action, estimated_cost)

    def erp_update(self, action, payload):
        return self._inner.erp_update(action, payload)
