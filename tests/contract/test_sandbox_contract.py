"""Track A §5, sandbox items 1 and 2.

Every read returns schema-valid responses for the seed data, and StubSandbox
and HttpSandbox return structurally identical shapes for the same query.

Shape is what is asserted. Where both sandboxes carry the same engineered
row — the COMP-104 catalog, PROD-882/PROD-914, PO-7712 — the values are
asserted too, because those rows are the test suite (§4.7) and a divergence
between the two sandboxes on them is exactly the bug this file exists to
catch before hour 12.
"""

from datetime import datetime

import pytest

from contracts.models import (
    ApprovalResult, Component, Message, ProductionOrder, PurchaseOrder, Quote,
    Supplier, TrackingRecord,
)

CATALOG = {
    "SUP-21": (118.0, 6, 1000, 0.91, 0.72, 200, ["ISO-9001", "Automotive-Grade"]),
    "SUP-42": (132.0, 4, 700, 0.94, 0.81, 300, ["ISO-9001", "Automotive-Grade"]),
    "SUP-37": (141.0, 6, 400, 0.96, 0.88, 100, ["ISO-9001", "Automotive-Grade"]),
    "SUP-18": (104.0, 3, 900, 0.79, 0.65, 250, ["ISO-9001"]),
    "SUP-55": (126.0, 5, 350, 0.92, 0.55, 350, ["ISO-9001", "Automotive-Grade"]),
}


def test_inventory_shape_and_the_numbers_that_matter(sandbox):
    items = sandbox.get_inventory()
    assert items and all(isinstance(c, Component) for c in items)

    comp = sandbox.get_inventory("COMP-104")[0]
    assert comp.current_stock == 420 and comp.usable_stock == 390
    assert comp.daily_usage == 90 and comp.safety_stock == 150
    assert comp.usable_stock / comp.daily_usage == pytest.approx(4.333, abs=0.001)
    assert comp.required_certifications == ["ISO-9001", "Automotive-Grade"]
    assert comp.min_quality == 0.90


def test_supplier_catalog_is_the_engineered_five_rows(sandbox):
    """§4.7: randomly generated suppliers would not contain this shape, so
    H-03 and H-05 would silently never fire."""
    suppliers = sandbox.get_suppliers("COMP-104")
    assert all(isinstance(s, Supplier) for s in suppliers)
    assert {s.supplier_id for s in suppliers} == set(CATALOG)

    for s in suppliers:
        price, lead, avail, quality, reliab, moq, certs = CATALOG[s.supplier_id]
        assert (s.unit_price, s.lead_time_days, s.available_quantity) == (price, lead, avail)
        assert (s.quality_score, s.reliability_score) == (quality, reliab)
        assert s.min_order_quantity == moq
        assert s.certifications == certs


def test_sup18_is_cheapest_and_fastest_and_uncertified(sandbox):
    """The trap. If this ever passes the certification filter, H-03 is dead."""
    suppliers = sandbox.get_suppliers("COMP-104")
    comp = sandbox.get_inventory("COMP-104")[0]
    sup18 = next(s for s in suppliers if s.supplier_id == "SUP-18")

    assert sup18.unit_price == min(s.unit_price for s in suppliers)
    assert sup18.lead_time_days == min(s.lead_time_days for s in suppliers)
    assert not set(comp.required_certifications) <= set(sup18.certifications)
    assert sup18.quality_score < comp.min_quality


def test_suppliers_for_an_unknown_component_is_empty_not_everything(sandbox):
    assert sandbox.get_suppliers("COMP-NOPE") == []


def test_purchase_orders_shape(sandbox):
    orders = sandbox.get_purchase_orders()
    assert orders and all(isinstance(p, PurchaseOrder) for p in orders)

    po = sandbox.get_purchase_orders("PO-7712")[0]
    assert po.component_id == "COMP-104" and po.supplier_id == "SUP-21"
    assert po.status == "delayed"
    assert po.approval_required_above == 150000


def test_production_schedule_carries_the_reschedule_pair(sandbox):
    orders = sandbox.get_production_schedule()
    assert all(isinstance(p, ProductionOrder) for p in orders)
    by_id = {p.production_order_id: p for p in orders}
    assert {"PROD-882", "PROD-914"} <= set(by_id)

    high, low = by_id["PROD-882"], by_id["PROD-914"]
    assert (high.priority, high.max_delay_days) == ("high", 0)
    assert (low.priority, low.max_delay_days) == ("low", 5)
    assert low.deadline < high.deadline, (
        "PROD-914 must fall due before PROD-882 or rung 2 never binds")


def test_tracking_contradicts_the_supplier_claim(sandbox):
    """Ground truth for A-3 / H-08."""
    record = sandbox.get_tracking("PO-7712")
    assert isinstance(record, TrackingRecord)
    assert record.supplier_claim == "dispatched"
    assert record.tracking_status == "label_created_no_pickup"
    assert record.last_movement is None


def test_inbox_shape(sandbox):
    messages = sandbox.get_inbox()
    assert messages and all(isinstance(m, Message) for m in messages)
    first = messages[0]
    assert first.related_po_id == "PO-7712"
    assert "delayed by 5-7 days" in first.body

    later = sandbox.get_inbox(since=datetime(2030, 1, 1))
    assert later == []


def test_send_message_returns_a_message(sandbox):
    sent = sandbox.send_message("SUP-21", "PO-7712 status", "Confirm the date?")
    assert isinstance(sent, Message)
    assert sent.recipient.endswith("@example.com")
    assert sent.subject == "PO-7712 status"


def test_rfq_quotes_carry_an_expiry_window(sandbox):
    quotes = sandbox.request_rfq("COMP-104", 700, 4, ["SUP-42", "SUP-37"])
    assert {q.supplier_id for q in quotes} == {"SUP-42", "SUP-37"}
    for q in quotes:
        assert isinstance(q, Quote)
        assert q.quote_valid_hours > 0, "G8 needs a window to expire against"
        assert q.quantity_available <= 700


def test_rfq_ignores_suppliers_that_do_not_carry_the_component(sandbox):
    assert sandbox.request_rfq("COMP-104", 100, 4, ["SUP-NOPE"]) == []


def test_approval_check_is_exactly_ps_5_8(sandbox):
    over = sandbox.check_approval("create_alternate_po", 150000.01)
    assert isinstance(over, ApprovalResult)
    assert over.approval_required is True
    assert over.approval_reason == (
        "Cost exceeds autonomous purchase threshold of 150000")

    at_threshold = sandbox.check_approval("create_alternate_po", 150000.0)
    assert at_threshold.approval_required is False
    assert at_threshold.approval_reason is None


@pytest.mark.parametrize("action", [
    "mark_po_delayed", "create_alternate_po", "attach_supplier_note",
    "update_production_risk", "record_escalation", "store_recovery_plan",
])
def test_erp_update_accepts_the_six_ps_5_9_actions(sandbox, action):
    result = sandbox.erp_update(action, {"note": "contract test"})
    assert result["status"] == "ok"
    assert result["record_id"]


@pytest.mark.parametrize("action", ["wire_transfer", "delete_supplier", "", "MARK_PO_DELAYED"])
def test_erp_update_rejects_everything_else(sandbox, action):
    result = sandbox.erp_update(action, {})
    assert result["status"] == "rejected"
    assert result["record_id"] is None
