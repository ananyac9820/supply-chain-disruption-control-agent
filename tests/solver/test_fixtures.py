"""Track A §5 items 1-7 — the solver half of the definition of done.

Fixture data lives in solver/fixtures/ (§8.2); the assertions live here (§5).
Every expected output in those files was hand-computed from the §4.1
objective, so these tests check the solver against the specification rather
than against itself. See solver/fixtures/README.md for the working.

Run from anywhere in the repo: pyproject.toml puts the root on the path.
"""

import json
from pathlib import Path

import pytest

from contracts.models import SolverInput, SolverOutput
from solver import solve

FIXTURES = Path(__file__).resolve().parents[2] / "solver" / "fixtures"

CASES = [
    "f01_simple", "f02_split", "f03_moq_overbuy", "f04_uncertified",
    "f05_reschedule", "f06_infeasible", "f07_trust_before", "f07_trust_after",
]


def load(name: str) -> tuple[SolverInput, SolverOutput]:
    inp = SolverInput.model_validate(json.loads((FIXTURES / f"{name}.json").read_text()))
    exp = SolverOutput.model_validate(
        json.loads((FIXTURES / f"{name}.expected.json").read_text()))
    return inp, exp


def allocations(out: SolverOutput) -> set[tuple[str, int, float]]:
    """Order is not part of the contract, so compare as a set."""
    return {(a.supplier_id, a.units, a.cost) for a in out.allocations}


def reschedules(out: SolverOutput) -> set[tuple[str, int]]:
    return {(r.production_order_id, r.delay_days) for r in out.reschedules}


def status_matches(expected: str, actual: str) -> bool:
    """OPTIMAL and FEASIBLE are interchangeable.

    CP-SAT proves optimality and reports OPTIMAL; the greedy fallback cannot
    prove anything and reports FEASIBLE for the same plan. The distinction is
    about the solver's confidence, not about whether the plan is right.
    """
    if expected == actual:
        return True
    return {expected, actual} == {"OPTIMAL", "FEASIBLE"}


@pytest.mark.parametrize("name", CASES)
def test_fixture_matches_hand_computed_expectation(name):
    inp, exp = load(name)
    got = solve(inp)

    assert status_matches(exp.status, got.status), (
        f"{name}: expected {exp.status}, got {got.status}")
    assert allocations(got) == allocations(exp), f"{name}: allocations differ"
    assert reschedules(got) == reschedules(exp), f"{name}: reschedules differ"
    assert got.total_cost == pytest.approx(exp.total_cost), f"{name}: cost differs"
    assert got.requires_approval == exp.requires_approval
    assert got.binding_constraint == exp.binding_constraint
    assert got.relaxation_used == exp.relaxation_used


# --- the specific property each case exists to prove ---------------------

def test_f01_single_supplier_hand_check():
    """§5.1 — gap 400, three suppliers, one correct answer."""
    got = solve(load("f01_simple")[0])
    assert allocations(got) == {("SUP-A", 400, 40000.0)}


def test_f02_splits_across_two_suppliers_respecting_moq():
    """§5.2 — no single supplier has enough."""
    inp = load("f02_split")[0]
    got = solve(inp)
    assert len(got.allocations) == 2
    moq = {s.supplier_id: s.min_order_quantity for s in inp.suppliers}
    for a in got.allocations:
        assert a.units >= moq[a.supplier_id], f"{a.supplier_id} below its MOQ"


def test_f03_overbuys_to_moq_only_because_the_total_still_wins():
    """§5.3 — the gap is 200 and the winning order is 500."""
    inp = load("f03_moq_overbuy")[0]
    got = solve(inp)
    gap = (inp.production_orders[0].units_required
           - (inp.usable_stock - inp.safety_stock))
    assert gap == 200
    assert allocations(got) == {("SUP-A", 500, 50000.0)}
    assert got.total_cost < 54000.0, "buying exactly 200 from SUP-B would cost more"


def test_f04_uncertified_supplier_is_absent_entirely():
    """§5.4 — SUP-18 is cheapest AND fastest, and must not appear at all."""
    inp = load("f04_uncertified")[0]
    got = solve(inp)
    sup18 = next(s for s in inp.suppliers if s.supplier_id == "SUP-18")
    assert sup18.unit_price == min(s.unit_price for s in inp.suppliers)
    assert sup18.lead_time_days == min(s.lead_time_days for s in inp.suppliers)
    assert "SUP-18" not in {a.supplier_id for a in got.allocations}


def test_f05_reschedule_is_the_only_feasible_recovery():
    """§5.5 — procurement alone is infeasible; freeing r[p] is not."""
    inp, exp = load("f05_reschedule")
    assert inp.allow_reschedule

    procurement_only = solve(inp.model_copy(update={"allow_reschedule": False}))
    assert procurement_only.status == "INFEASIBLE"
    assert procurement_only.binding_constraint == "deadline"

    got = solve(inp)
    assert got.status != "INFEASIBLE"
    assert got.relaxation_used == "reschedule"
    assert reschedules(got) == {("PROD-914", 4)}
    delayed = {r.production_order_id for r in got.reschedules}
    high = {p.production_order_id for p in inp.production_orders
            if p.max_delay_days == 0}
    assert not (delayed & high), "a non-delayable order was rescheduled"


def test_f06_infeasible_names_a_real_binding_constraint():
    """§5.6 — never a bare INFEASIBLE; the brief needs the reason."""
    got = solve(load("f06_infeasible")[0])
    assert got.status == "INFEASIBLE"
    assert got.binding_constraint == "available_quantity"
    assert got.allocations == []


def test_f07_trust_changes_the_allocation_within_a_run():
    """§5.7 — identical input, one contradicted claim between, different answer.

    §4.5: a ledger that never alters a decision is worth nothing.
    """
    before_in, _ = load("f07_trust_before")
    after_in, _ = load("f07_trust_after")

    differing = [(b.supplier_id, b.effective_reliability, a.effective_reliability)
                 for b, a in zip(before_in.suppliers, after_in.suppliers)
                 if b.effective_reliability != a.effective_reliability]
    assert differing == [("SUP-X", 0.90, 0.75)], (
        "the two inputs must differ in effective_reliability and nothing else")

    before, after = solve(before_in), solve(after_in)
    assert allocations(before) != allocations(after)
    assert {a.supplier_id for a in before.allocations} == {"SUP-X"}
    assert {a.supplier_id for a in after.allocations} == {"SUP-Y"}


def test_every_fixture_file_validates_against_the_frozen_contract():
    """A fixture that drifts from contracts/models.py is worse than no fixture."""
    files = sorted(FIXTURES.glob("*.json"))
    assert len(files) == 2 * len(CASES)
    for f in files:
        payload = json.loads(f.read_text())
        if f.name.endswith(".expected.json"):
            SolverOutput.model_validate(payload)
        else:
            SolverInput.model_validate(payload)
