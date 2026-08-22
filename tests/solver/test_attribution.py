"""Incidents, attribution, and the two axes staying separate.

The rule this file exists to hold: an incident is recorded for every
discrepancy, always; reputation moves only when the evidence attributes the
discrepancy to the supplier.
"""

from datetime import datetime, timedelta

import pytest

from sandbox import attribution, db
from trust import (effective_reliability, incidents_for, record_incident,
                   reputation, shipment_confidence, trust_reset,
                   units_confirmed, UNCONFIRMED_BELOW)

NOW = datetime(2026, 9, 2, 10, 0, 0)


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "attr.db")
    db.init_db(reset=True)
    trust_reset()
    yield


def row(**overrides) -> dict:
    base = {"po_id": "PO-7712", "supplier_claim": "dispatched",
            "tracking_status": "label_created_no_pickup", "last_movement": None,
            "packed_at": None, "tendered_at": None}
    base.update(overrides)
    return base


class Row(dict):
    """sqlite3.Row-alike: attribution.classify calls .keys()."""


def as_row(**overrides) -> Row:
    return Row(row(**overrides))


# ---- what the evidence can and cannot establish --------------------------

def test_label_with_no_pickup_is_unattributed():
    """The case the whole change is about.

    A printed label and no collection scan is equally consistent with a
    supplier that never handed the goods over and a courier that never came.
    Nothing in the record separates them.
    """
    verdict, basis, observed, expected = attribution.classify(as_row(), NOW)
    assert verdict == "UNATTRIBUTED"
    assert "never tendered" in basis and "never made" in basis


def test_nothing_tendered_and_no_pack_event_is_the_suppliers():
    verdict, basis, _, _ = attribution.classify(
        as_row(tracking_status="not_found", packed_at=None), NOW)
    assert verdict == "SUPPLIER"
    assert "no pack event" in basis


def test_a_pack_event_makes_even_a_missing_consignment_ambiguous():
    verdict, _, _, _ = attribution.classify(
        as_row(tracking_status="not_found",
               packed_at=(NOW - timedelta(days=1)).isoformat()), NOW)
    assert verdict == "UNATTRIBUTED"


def test_picked_up_then_stalled_is_the_couriers():
    verdict, basis, _, _ = attribution.classify(
        as_row(tracking_status="in_transit",
               tendered_at=(NOW - timedelta(days=4)).isoformat(),
               last_movement=(NOW - timedelta(days=3)).isoformat()), NOW)
    assert verdict == "COURIER"
    assert "no movement" in basis


def test_a_recent_handover_is_too_early_to_attribute():
    verdict, _, _, _ = attribution.classify(
        as_row(tracking_status="in_transit",
               tendered_at=(NOW - timedelta(hours=2)).isoformat(),
               last_movement=(NOW - timedelta(hours=2)).isoformat()), NOW)
    assert verdict == "UNATTRIBUTED"


def test_a_supported_claim_is_not_a_discrepancy():
    assert not attribution.has_discrepancy(as_row(tracking_status="in_transit"))
    assert not attribution.has_discrepancy(as_row(tracking_status="delivered"))
    assert attribution.has_discrepancy(as_row())


# ---- the two axes --------------------------------------------------------

@pytest.mark.parametrize("verdict", ["UNATTRIBUTED", "COURIER", "EXTERNAL", "FACTORY"])
def test_an_unattributed_incident_is_recorded_but_moves_no_reputation(verdict):
    before = effective_reliability("SUP-21", 0.72)
    record_incident("PO-7712", "SUP-21", "observed", "expected", verdict, "basis")

    assert len(incidents_for("PO-7712")) == 1, "recorded regardless of fault"
    assert shipment_confidence("PO-7712") < 1.0, "the shipment is still unverifiable"
    assert not units_confirmed("PO-7712")
    assert effective_reliability("SUP-21", 0.72) == before, "reputation untouched"
    assert reputation("SUP-21").contradicted_claims == 0


def test_a_supplier_attributed_incident_moves_both_axes():
    before = effective_reliability("SUP-21", 0.72)
    record_incident("PO-7712", "SUP-21", "observed", "expected", "SUPPLIER",
                    "no consignment tendered and no pack event")

    assert len(incidents_for("PO-7712")) == 1
    assert shipment_confidence("PO-7712") < 1.0
    assert effective_reliability("SUP-21", 0.72) < before
    assert reputation("SUP-21").contradicted_claims == 1


def test_confidence_is_per_shipment_not_per_supplier():
    """One bad consignment does not condemn a supplier's other shipments."""
    record_incident("PO-7712", "SUP-21", "o", "e", "UNATTRIBUTED", "b")
    assert not units_confirmed("PO-7712")
    assert units_confirmed("PO-9999"), "a different PO is unaffected"


def test_recording_is_idempotent_per_observation():
    for _ in range(5):
        record_incident("PO-7712", "SUP-21", "same observation", "e",
                        "SUPPLIER", "b")
    assert len(incidents_for("PO-7712")) == 1
    assert reputation("SUP-21").contradicted_claims == 1, (
        "polling tracking must not compound either axis")


def test_an_unknown_attribution_is_refused():
    with pytest.raises(ValueError):
        record_incident("PO-7712", "SUP-21", "o", "e", "PROBABLY_THEM", "b")


def test_the_confidence_threshold_agrees_with_the_guardrail():
    """guardrails/ holds no dependency on the ledger, so the two constants are
    defined separately. They must not drift."""
    from guardrails.rules import DEFAULT_UNCONFIRMED_BELOW
    assert UNCONFIRMED_BELOW == DEFAULT_UNCONFIRMED_BELOW


def test_no_accusatory_language_reaches_a_guardrail_reason():
    from contracts.models import Allocation, SolverOutput
    from guardrails.validator import validate

    plan = SolverOutput(status="FEASIBLE", total_cost=1000.0,
                        allocations=[Allocation(supplier_id="SUP-21", units=100,
                                                cost=1000.0, arrival_day=6)])
    verdict = validate(plan, {"approval_limit": 150000.0,
                              "shipment_confidence": {"SUP-21": 0.4}})
    assert "G9" in verdict.fired
    reason = verdict.reasons[0].lower()
    assert "unconfirmed" in reason
    for word in ("lie", "lied", "lying", "dishonest", "false", "contradicted"):
        assert word not in reason, f"{word!r} implies a finding we have not made"
