"""The other five post-checks — G1, G5, G8, G9, G12 — and the five that must
never appear at all.

§5 item 8 (G2) lives in test_guardrails.py. This file covers the rest of
§4.4's post-check half.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from contracts.models import Allocation, SolverInput, SolverOutput
from guardrails.validator import unreachable_pre_solve_rules, validate, vetoed
from solver import solve

FIXTURES = Path(__file__).resolve().parents[2] / "solver" / "fixtures"
NOW = datetime(2026, 9, 2, 10, 0, 0)


def plan(cost: float, *suppliers: str, status: str = "FEASIBLE") -> SolverOutput:
    return SolverOutput(
        status=status,
        allocations=[Allocation(supplier_id=s, units=100, cost=cost / len(suppliers),
                                arrival_day=4) for s in suppliers] if suppliers else [],
        total_cost=cost,
        requires_approval=cost > 150000,
        relaxation_used="none",
    )


BASE = {"approval_limit": 150000.0, "now": NOW}


# ---- G1 -----------------------------------------------------------------

def test_g1_fires_when_the_plan_outruns_the_remaining_budget():
    verdict = validate(plan(90000.0, "SUP-42"), BASE | {"remaining_budget": 50000.0})
    assert "G1" in verdict.fired
    assert not verdict.passed
    assert vetoed(verdict), "G1 means re-solve under a tighter cap"
    assert "40,000" in verdict.reasons[0], "the reason should name the overage"


def test_g1_silent_when_the_plan_fits():
    verdict = validate(plan(40000.0, "SUP-42"), BASE | {"remaining_budget": 50000.0})
    assert verdict.fired == []
    assert verdict.passed


def test_g1_does_not_fire_when_the_caller_supplies_no_budget():
    """A rule whose inputs are absent must not fire. Node 4 may call validate
    before it knows the remaining budget."""
    assert validate(plan(999999.0, "SUP-42"), {"approval_limit": 1e12}).fired == []


# ---- G5 -----------------------------------------------------------------

def test_g5_vetoes_an_unjustified_safety_stock_breach():
    verdict = validate(plan(1000.0, "SUP-42"), BASE | {
        "projected_stock": 90, "safety_stock": 150})
    assert "G5" in verdict.fired
    assert vetoed(verdict)
    assert "60 short" in verdict.reasons[0]


def test_g5_escalates_without_vetoing_when_justified():
    verdict = validate(plan(1000.0, "SUP-42"), BASE | {
        "projected_stock": 90, "safety_stock": 150,
        "safety_stock_breach_justification": "line stops in 12h, PROD-882 high"})
    assert "G5" in verdict.fired
    assert not vetoed(verdict), "a justified breach is a decision, not an error"


def test_g5_always_escalates_when_a_high_priority_order_is_affected():
    verdict = validate(plan(1000.0, "SUP-42"), BASE | {
        "projected_stock": 90, "safety_stock": 150,
        "safety_stock_breach_justification": "documented",
        "affected_priorities": ["high", "low"]})
    assert verdict.forced_escalation is True


def test_g5_silent_when_the_floor_is_preserved():
    verdict = validate(plan(1000.0, "SUP-42"), BASE | {
        "projected_stock": 200, "safety_stock": 150})
    assert verdict.fired == []


# ---- G8 -----------------------------------------------------------------

def test_g8_invalidates_only_the_supplier_whose_quote_expired():
    context = BASE | {"quotes": [
        {"supplier_id": "SUP-42", "issued_at": NOW - timedelta(hours=7),
         "quote_valid_hours": 6},
        {"supplier_id": "SUP-37", "issued_at": NOW - timedelta(hours=1),
         "quote_valid_hours": 6},
    ]}
    verdict = validate(plan(1000.0, "SUP-42", "SUP-37"), context)
    assert "G8" in verdict.fired
    assert "SUP-42" in verdict.reasons[0]
    assert "SUP-37" not in verdict.reasons[0], "G8 invalidates one quote, not the plan"


def test_g8_silent_inside_the_validity_window():
    context = BASE | {"quotes": [
        {"supplier_id": "SUP-42", "issued_at": NOW - timedelta(hours=5),
         "quote_valid_hours": 6}]}
    assert validate(plan(1000.0, "SUP-42"), context).fired == []


# ---- G9 -----------------------------------------------------------------

def test_g9_fires_when_the_plan_leans_on_a_contradicted_claim():
    context = BASE | {"claims": [
        {"supplier_id": "SUP-21", "status": "CONTRADICTED"},
        {"supplier_id": "SUP-42", "status": "GROUNDED"}]}
    verdict = validate(plan(1000.0, "SUP-21", "SUP-42"), context)
    assert "G9" in verdict.fired
    assert vetoed(verdict)
    assert "SUP-21" in verdict.reasons[0] and "SUP-42" not in verdict.reasons[0]


@pytest.mark.parametrize("status", ["GROUNDED", "UNVERIFIABLE"])
def test_g9_only_fires_on_contradicted(status):
    context = BASE | {"claims": [{"supplier_id": "SUP-21", "status": status}]}
    assert validate(plan(1000.0, "SUP-21"), context).fired == []


# ---- G12 ----------------------------------------------------------------

def test_g12_escalates_on_a_real_infeasible_solver_output():
    """Driven from f06 rather than a hand-built object, so this breaks if the
    solver ever stops naming a binding constraint."""
    inp = SolverInput.model_validate(
        json.loads((FIXTURES / "f06_infeasible.json").read_text()))
    out = solve(inp)
    assert out.status == "INFEASIBLE"

    verdict = validate(out, BASE)
    assert "G12" in verdict.fired
    assert verdict.forced_escalation is True
    assert not vetoed(verdict), "the ladder already ran out; re-solving is futile"
    assert "available_quantity" in verdict.reasons[0]


# ---- the five that must never appear -------------------------------------

def test_pre_solve_rules_never_appear_in_a_verdict():
    """G3, G4, G6, G7 and G11 are filters and model constraints.

    A plan breaking them cannot be constructed, so if one shows up here the
    bug is in the model, not in the plan (§4.4).
    """
    contexts = [
        BASE,
        BASE | {"remaining_budget": 1.0},
        BASE | {"projected_stock": 0, "safety_stock": 150},
        BASE | {"claims": [{"supplier_id": "SUP-21", "status": "CONTRADICTED"}]},
    ]
    for context in contexts:
        verdict = validate(plan(200000.0, "SUP-21", "SUP-42"), context)
        assert not set(verdict.fired) & set(unreachable_pre_solve_rules())


def test_a_clean_plan_passes_with_nothing_fired():
    verdict = validate(plan(100000.0, "SUP-42"), BASE | {
        "remaining_budget": 400000.0, "projected_stock": 200, "safety_stock": 150})
    assert verdict.passed and verdict.fired == [] and not verdict.forced_escalation
