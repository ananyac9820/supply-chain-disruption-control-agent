"""The CP-SAT recovery model — §4.1 variables, constraints and objective,
walking the §4.2 relaxation ladder.

Two decision families, which is the whole differentiator: x[s] units bought
from supplier s, and r[p] days production order p slips. A sourcing-only
model declares infeasible the scenarios this one solves.

Money
-----
Every monetary quantity inside the model is an integer number of paise.
CP-SAT is an integer solver; handing it floats produces a model that runs,
returns a plausible answer, and is quietly wrong. Conversion happens once, at
the boundary, and results are divided back out on the way to SolverOutput.

C6 is cumulative
----------------
§4.1 prints C6 per order, comparing each order independently against the same
usable_stock. That double-counts stock across competing orders and makes §7
B-4 unrepresentable; it is ruled a spec bug in solver/fixtures/README.md.

The cumulative form is encoded without needing to know the order in which
production orders fall due — which is itself a decision variable, since r[p]
moves deadlines around. Instead, for every candidate deadline day T:

    units required by T  <=  (usable_stock - safety_stock) + units arriving by T

Demand at T is a linear expression over the delay indicators, and supply at T
is a sum over suppliers whose lead time clears T. Checking this at every
candidate deadline is sufficient: demand is a step function that only rises
at those days, and supply never falls.
"""

from ortools.sat.python import cp_model

from contracts.constants import W_LATE, W_RISK, W_SPLIT
from contracts.models import (
    Allocation, Reschedule, SolverInput, SolverOutput, SolverSupplier,
)
from solver.fallback import BINDING  # the shared binding-constraint vocabulary

PAISE = 100

# Per-unit penalty for uncovered demand on the partial rung. Any value that
# dominates a unit price works; W_LATE is already the "a production day is
# worth this much" number, so borrowing it keeps the scale defensible.
SHORTFALL_PENALTY = W_LATE * PAISE

SOLVE_TIME_LIMIT_SECONDS = 10.0


class _Relax:
    """Which constraint family to drop. Used only by _diagnose()."""

    __slots__ = ("certification", "moq", "availability", "deadline", "budget")

    def __init__(self, certification=False, moq=False, availability=False,
                 deadline=False, budget=False):
        self.certification = certification
        self.moq = moq
        self.availability = availability
        self.deadline = deadline
        self.budget = budget


def solve(inp: SolverInput) -> SolverOutput:
    """The four-rung ladder of §4.2. Never crashes, never returns nothing."""
    out = _solve(inp, allow_reschedule=False, allow_partial=False)   # rung 1
    if out is not None:
        out.relaxation_used = "none"
        return out

    if inp.allow_reschedule:                                          # rung 2
        out = _solve(inp, allow_reschedule=True, allow_partial=False)
        if out is not None:
            out.relaxation_used = "reschedule"
            return out

    if inp.allow_partial:                                             # rung 3
        out = _solve(inp, allow_reschedule=inp.allow_reschedule,
                     allow_partial=True)
        if out is not None:
            out.relaxation_used = "partial"
            return out

    return SolverOutput(                                              # rung 4
        status="INFEASIBLE",
        binding_constraint=_diagnose(inp),
        relaxation_used=None,
    )


# ---- the model ---------------------------------------------------------

def _solve(inp: SolverInput, allow_reschedule: bool, allow_partial: bool,
           relax: _Relax | None = None) -> SolverOutput | None:
    """Build and solve one rung. Returns None when the rung is infeasible."""
    relax = relax or _Relax()
    model = cp_model.CpModel()
    suppliers = list(inp.suppliers)
    orders = list(inp.production_orders)

    # --- decision variables ---
    avail = {s.supplier_id: (10 ** 7 if relax.availability else s.available_quantity)
             for s in suppliers}
    x = {s.supplier_id: model.NewIntVar(0, avail[s.supplier_id], f"x[{s.supplier_id}]")
         for s in suppliers}
    y = {s.supplier_id: model.NewBoolVar(f"y[{s.supplier_id}]") for s in suppliers}

    delays = {p.production_order_id:
              list(range(p.max_delay_days + 1)) if allow_reschedule else [0]
              for p in orders}
    # One indicator per (order, candidate delay). r[p] is recovered from them,
    # and they are what makes the cumulative constraint linear.
    pick = {p.production_order_id: {d: model.NewBoolVar(f"d[{p.production_order_id}={d}]")
                                    for d in delays[p.production_order_id]}
            for p in orders}
    r = {}
    for p in orders:
        model.AddExactlyOne(pick[p.production_order_id].values())
        r[p.production_order_id] = model.NewIntVar(
            0, max(delays[p.production_order_id]), f"r[{p.production_order_id}]")
        model.Add(r[p.production_order_id] == sum(
            d * var for d, var in pick[p.production_order_id].items()))

    for s in suppliers:
        sid = s.supplier_id
        model.Add(x[sid] <= avail[sid] * y[sid])                       # C1
        if not relax.moq:
            model.Add(x[sid] >= s.min_order_quantity * y[sid])         # C2
        if not relax.certification:
            if not s.certified:
                model.Add(y[sid] == 0)                                 # C3
            if s.quality_score < inp.min_quality:
                model.Add(y[sid] == 0)                                 # C4
        if s.quote_expired:
            model.Add(y[sid] == 0)                                     # C7
        if s.claim_contradicted:
            model.Add(y[sid] == 0)                                     # C8

    if not relax.budget:                                               # C5
        model.Add(sum(x[s.supplier_id] * _paise(s.unit_price) for s in suppliers)
                  <= _paise(inp.budget_cap))

    # --- C6, cumulative ---
    on_hand = inp.usable_stock - inp.safety_stock
    total_demand = sum(p.units_required for p in orders)
    short = model.NewIntVar(0, max(total_demand, 0), "shortfall") \
        if allow_partial else None

    for day in _candidate_days(orders, delays):
        demand = sum(
            p.units_required * pick[p.production_order_id][d]
            for p in orders for d in delays[p.production_order_id]
            if p.deadline_day + d <= day)
        arriving = sum(x[s.supplier_id] for s in suppliers
                       if relax.deadline or s.lead_time_days <= day)
        if short is None:
            model.Add(demand <= on_hand + arriving)
        else:
            model.Add(demand - short <= on_hand + arriving)

    # --- objective, entirely in paise ---
    terms = [x[s.supplier_id] * _paise(s.unit_price) for s in suppliers]
    terms += [int(round(W_LATE * PAISE * p.priority_weight)) * r[p.production_order_id]
              for p in orders]
    terms += [int(round(W_RISK * PAISE * (1 - s.effective_reliability)))
              * x[s.supplier_id] for s in suppliers]
    terms += [W_SPLIT * PAISE * y[s.supplier_id] for s in suppliers]
    if short is not None:
        terms.append(SHORTFALL_PENALTY * short)
    model.Minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = SOLVE_TIME_LIMIT_SECONDS
    solver.parameters.num_search_workers = 1        # deterministic across runs
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None

    return _extract(inp, solver, suppliers, orders, x, r, status)


def _extract(inp: SolverInput, solver, suppliers: list[SolverSupplier], orders,
             x, r, status) -> SolverOutput:
    allocations = []
    total_paise = 0
    for s in suppliers:
        units = solver.Value(x[s.supplier_id])
        if units <= 0:
            continue
        cost_paise = units * _paise(s.unit_price)
        total_paise += cost_paise
        allocations.append(Allocation(
            supplier_id=s.supplier_id, units=units,
            cost=cost_paise / PAISE, arrival_day=s.lead_time_days))

    reschedules = [Reschedule(production_order_id=p.production_order_id,
                              delay_days=solver.Value(r[p.production_order_id]))
                   for p in orders if solver.Value(r[p.production_order_id]) > 0]

    total_cost = total_paise / PAISE
    return SolverOutput(
        status="OPTIMAL" if status == cp_model.OPTIMAL else "FEASIBLE",
        allocations=sorted(allocations, key=lambda a: a.supplier_id),
        reschedules=sorted(reschedules, key=lambda r_: r_.production_order_id),
        total_cost=total_cost,
        requires_approval=total_cost > inp.approval_limit,
    )


def _candidate_days(orders, delays) -> list[int]:
    """Every day on which demand can step up. Checking these is sufficient."""
    return sorted({p.deadline_day + d
                   for p in orders for d in delays[p.production_order_id]})


def _paise(rupees: float) -> int:
    """The single float boundary in this module."""
    return int(round(rupees * PAISE))


# ---- rung 4: name the constraint that actually bound --------------------

def _diagnose(inp: SolverInput) -> str:
    """Relax one constraint family at a time; report the one that rescues it.

    Ordering matters when more than one family independently restores
    feasibility: report the levers an ops manager can pull before the ones
    they cannot. Certification is last because G3 is a compliance filter
    nobody will relax, and naming it reads as advice to ship uncertified
    parts. It still surfaces when it is the only thing that binds.
    """
    for name, relax in (
        ("budget", _Relax(budget=True)),
        ("deadline", _Relax(deadline=True)),
        ("available_quantity", _Relax(availability=True)),
        ("moq", _Relax(moq=True)),
        ("certification", _Relax(certification=True)),
    ):
        probe = _solve(inp, allow_reschedule=inp.allow_reschedule,
                       allow_partial=False, relax=relax)
        if probe is not None:
            return name

    # Nothing single-handedly rescues it: several families bind at once.
    # Report the scarcest resource rather than inventing a winner.
    return "available_quantity"


assert set(BINDING) == {"budget", "deadline", "certification",
                        "available_quantity", "moq"}
