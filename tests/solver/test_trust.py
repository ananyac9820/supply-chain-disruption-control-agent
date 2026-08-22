"""§4.5 — the ledger, and the only property that makes it worth having.

f07 pins the behaviour with hand-set reliabilities. This file drives the real
ledger, so it also proves the arithmetic in trust.py produces the numbers f07
assumes.
"""

import pytest

from contracts.constants import W_RISK
from contracts.models import SolverInput, SolverProdOrder, SolverSupplier
from sandbox import db
from solver import fallback, model
from trust import (RELIABILITY_FLOOR, effective_reliability, trust_read,
                   trust_reset, trust_write)


@pytest.fixture(autouse=True)
def ledger(tmp_path, monkeypatch):
    """A private database per test. The ledger compounds by design, so a
    shared one would make results depend on test order."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "trust.db")
    db.init_db(reset=True)
    trust_reset()
    yield


def test_an_unknown_supplier_reads_as_a_clean_slate():
    t = trust_read("SUP-NEW")
    assert t.contradicted_claims == 0 and t.late_count == 0
    assert effective_reliability("SUP-NEW", 0.81) == 0.81


@pytest.mark.parametrize("event,field", [
    ("on_time", "on_time_count"),
    ("late", "late_count"),
    ("contradicted_claim", "contradicted_claims"),
    ("moq_failure", "moq_failures"),
])
def test_each_event_increments_its_own_counter(event, field):
    trust_write("SUP-21", event)
    trust_write("SUP-21", event)
    assert getattr(trust_read("SUP-21"), field) == 2


def test_penalties_match_section_4_5_exactly():
    trust_write("SUP-21", "contradicted_claim")     # 0.15
    trust_write("SUP-21", "late")                   # 0.05
    trust_write("SUP-21", "moq_failure")            # 0.05
    assert effective_reliability("SUP-21", 0.90) == pytest.approx(0.65)


def test_one_contradicted_claim_is_the_0_15_drop_f07_assumes():
    """f07 moves SUP-X from 0.90 to 0.75. That number comes from here."""
    trust_write("SUP-X", "contradicted_claim")
    assert effective_reliability("SUP-X", 0.90) == pytest.approx(0.75)


def test_reliability_never_falls_through_the_floor():
    for _ in range(20):
        trust_write("SUP-21", "contradicted_claim")
    assert effective_reliability("SUP-21", 0.90) == RELIABILITY_FLOOR


def test_on_time_deliveries_do_not_offset_an_attributed_failure():
    """§4.5's formula has no credit term. Ten clean deliveries do not undo one
    attributed failure — a deliberate asymmetry, not an omission."""
    trust_write("SUP-21", "contradicted_claim")
    penalised = effective_reliability("SUP-21", 0.90)
    for _ in range(10):
        trust_write("SUP-21", "on_time")
    assert effective_reliability("SUP-21", 0.90) == penalised
    assert trust_read("SUP-21").on_time_count == 10


def test_quality_miss_is_recorded_but_not_double_charged():
    """G4's floor already removes an under-quality supplier. Charging the risk
    term as well would price one failure twice."""
    trust_write("SUP-18", "quality_miss")
    assert trust_read("SUP-18").quality_delta == pytest.approx(0.05)
    assert effective_reliability("SUP-18", 0.65) == pytest.approx(0.65)


def test_unknown_event_is_refused():
    with pytest.raises(ValueError):
        trust_write("SUP-21", "vibes")


# ---- the property that makes the ledger worth building -------------------

def _input_from_ledger() -> SolverInput:
    """Two suppliers, one cheaper, one more reliable. Same shape as f07."""
    return SolverInput(
        component_id="COMP-407", usable_stock=500, safety_stock=100,
        daily_usage=80, min_quality=0.90,
        suppliers=[
            SolverSupplier(
                supplier_id="SUP-X", unit_price=100.0, lead_time_days=3,
                available_quantity=400, min_order_quantity=100,
                effective_reliability=effective_reliability("SUP-X", 0.90),
                certified=True, quality_score=0.95),
            SolverSupplier(
                supplier_id="SUP-Y", unit_price=105.0, lead_time_days=3,
                available_quantity=400, min_order_quantity=100,
                effective_reliability=effective_reliability("SUP-Y", 0.95),
                certified=True, quality_score=0.95),
        ],
        production_orders=[SolverProdOrder(
            production_order_id="PO-1", units_required=800, deadline_day=5,
            priority_weight=5.0, max_delay_days=0)],
        budget_cap=400000.0)


@pytest.mark.parametrize("solve", [model.solve, fallback.solve],
                         ids=["cp-sat", "greedy"])
def test_an_attributed_incident_changes_the_next_allocation(solve):
    """The whole point of §4.5, driven through the real ledger.

    Same catalog, same gap, same everything — except that between the two
    solves an incident attributed to SUP-X reaches its reputation.
    """
    before = solve(_input_from_ledger())
    assert {a.supplier_id for a in before.allocations} == {"SUP-X"}

    trust_write("SUP-X", "contradicted_claim")

    after = solve(_input_from_ledger())
    assert {a.supplier_id for a in after.allocations} == {"SUP-Y"}
    assert after.total_cost > before.total_cost, (
        "the agent knowingly pays more to route around an attributed failure")


def test_both_solvers_read_the_risk_weight_from_the_frozen_constant():
    """If these ever drift, the ledger changes one solver's answer and not the
    other's, and the fallback stops being a degraded version of the model."""
    assert fallback.W_RISK is W_RISK
    assert model.W_RISK is W_RISK
