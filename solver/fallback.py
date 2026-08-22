"""Greedy fallback allocator — Track A §4.3.

Two reasons this file exists.

1. It is what Track B codes against before the hour-12 merge. Person B is
   blocked on the *shape* of a SolverOutput, not on the quality of the
   answer inside it, so this ships on day one.
2. It is the insurance policy. If CP-SAT is misbehaving at hour 16, ship
   this and the system still works — less impressively, but it works.

Same SolverInput in, same SolverOutput out as solver/model.py. It also walks
the same four-rung relaxation ladder (§4.2) and names the same binding
constraints, so nothing in Track B's node 4 changes at the merge.

Deliberately not here: CP-SAT, and any claim of optimality. A greedy sweep
cannot prove a plan is best, so it reports FEASIBLE, never OPTIMAL, for any
plan that actually buys something.
"""

from dataclasses import dataclass

from contracts.models import (
    Allocation, Reschedule, SolverInput, SolverOutput, SolverSupplier,
)

# The binding-constraint vocabulary from §4.2. Person B puts these strings
# straight into the escalation brief, so they are a contract of their own.
BINDING = ("budget", "deadline", "certification", "available_quantity", "moq")


@dataclass(frozen=True)
class _Relax:
    """Which constraint family to ignore. Used only by _diagnose()."""
    certification: bool = False
    moq: bool = False
    availability: bool = False
    deadline: bool = False
    budget: bool = False


def solve(inp: SolverInput) -> SolverOutput:
    """The relaxation ladder of §4.2. Never crashes, never returns nothing."""
    out = _attempt(inp, delays=_no_delays(inp))                      # rung 1
    if out is not None:
        out.relaxation_used = "none"
        return out

    if inp.allow_reschedule:                                         # rung 2
        delays = _minimal_delays(inp)
        if delays is not None:
            out = _attempt(inp, delays=delays)
            if out is not None:
                out.relaxation_used = "reschedule"
                out.reschedules = [
                    Reschedule(production_order_id=pid, delay_days=d)
                    for pid, d in sorted(delays.items()) if d > 0
                ]
                return out

    if inp.allow_partial:                                            # rung 3
        delays = _minimal_delays(inp) if inp.allow_reschedule else _no_delays(inp)
        out = _attempt(inp, delays=delays or _no_delays(inp), partial=True)
        if out is not None:
            out.relaxation_used = "partial"
            return out

    return SolverOutput(                                             # rung 4
        status="INFEASIBLE",
        binding_constraint=_diagnose(inp),
        relaxation_used=None,
    )


# ---- the greedy sweep --------------------------------------------------

def _attempt(inp: SolverInput, delays: dict[str, int], partial: bool = False,
             relax: _Relax = _Relax()) -> SolverOutput | None:
    """Cheapest-first allocation against each deadline in turn.

    Returns None when the requirement cannot be met, so the ladder above can
    move to the next rung.
    """
    candidates = _eligible(inp, relax)
    remaining = {s.supplier_id: s.available_quantity for s in candidates}
    taken: dict[str, int] = {}
    spent = 0.0

    for day, needed in _requirements(inp, delays):
        # Units already bought that land on or before this deadline.
        arrived = sum(
            units for sid, units in taken.items()
            if relax.deadline or _by_id(candidates, sid).lead_time_days <= day
        )
        short = needed - arrived
        while short > 0:
            pick = _cheapest(candidates, remaining, day, short, relax)
            if pick is None:
                if partial:
                    break          # buy what we can, report it honestly
                return None
            # On the partial rung the budget is still hard (G1): buy what
            # fits under the cap rather than failing the whole plan.
            budget_left = (inp.budget_cap - spent
                           if partial and not relax.budget else None)
            units = _order_size(pick, remaining[pick.supplier_id], short, relax,
                                budget_left)
            if units is None:
                remaining[pick.supplier_id] = 0     # MOQ or budget — drop it
                continue
            taken[pick.supplier_id] = taken.get(pick.supplier_id, 0) + units
            remaining[pick.supplier_id] -= units
            spent += units * pick.unit_price
            short -= units

    allocations = [
        Allocation(
            supplier_id=sid,
            units=units,
            cost=round(units * _by_id(candidates, sid).unit_price, 2),
            arrival_day=_by_id(candidates, sid).lead_time_days,
        )
        for sid, units in sorted(taken.items())
    ]
    total = round(sum(a.cost for a in allocations), 2)

    if not relax.budget and total > inp.budget_cap:
        return None

    return SolverOutput(
        status="OPTIMAL" if not allocations else "FEASIBLE",
        allocations=allocations,
        total_cost=total,
        requires_approval=total > inp.approval_limit,
    )


def _by_id(candidates: list[SolverSupplier], supplier_id: str) -> SolverSupplier:
    return next(s for s in candidates if s.supplier_id == supplier_id)


def _eligible(inp: SolverInput, relax: _Relax) -> list[SolverSupplier]:
    """G3, G8 and G9 as pre-solve filters (§4.4).

    A supplier removed here is never shown downstream as "cheaper, with risk
    noted" — it is simply not a candidate. G4's quality floor is applied by
    the caller when it builds SolverInput, since min_quality lives on the
    Component and never reaches the solver.
    """
    return sorted(
        (s for s in inp.suppliers
         if (relax.certification or s.certified)
         and not s.quote_expired
         and not s.claim_contradicted),
        key=lambda s: (s.unit_price, s.lead_time_days, s.supplier_id),
    )


def _requirements(inp: SolverInput, delays: dict[str, int]) -> list[tuple[int, int]]:
    """(deadline_day, cumulative units that must have arrived by then).

    Safety stock is reserved, not spent — the §4.4 G5 rule. Orders are walked
    in deadline order so an early high-priority order cannot be satisfied with
    stock that a later one already consumed.
    """
    orders = sorted(
        inp.production_orders,
        key=lambda p: p.deadline_day + delays.get(p.production_order_id, 0),
    )
    on_hand = inp.usable_stock - inp.safety_stock
    demand = 0
    reqs = []
    for p in orders:
        demand += p.units_required
        reqs.append((p.deadline_day + delays.get(p.production_order_id, 0),
                     max(0, demand - on_hand)))
    return reqs


def _cheapest(candidates: list[SolverSupplier], remaining: dict[str, int],
              day: int, short: int, relax: _Relax) -> SolverSupplier | None:
    for s in candidates:                       # already sorted by unit price
        if remaining[s.supplier_id] <= 0:
            continue
        if not relax.deadline and s.lead_time_days > day:
            continue
        return s
    return None


def _order_size(s: SolverSupplier, available: int, short: int, relax: _Relax,
                budget_left: float | None = None) -> int | None:
    """MOQ is take-at-least-this or take-none (G6). None means take none.

    Overbuying to reach MOQ is allowed — the surplus is real inventory, and
    §7 A-8 wants it chosen when the total still wins. The greedy sweep cannot
    weigh that trade the way CP-SAT does; it overbuys whenever the MOQ fits.

    budget_left is set only on the partial rung. A supplier whose MOQ no
    longer fits under the remaining cap is dropped, not part-ordered.
    """
    if relax.moq:
        size = min(short, available)
    elif s.min_order_quantity > available:
        return None
    else:
        size = min(max(short, s.min_order_quantity), available)

    if budget_left is not None:
        affordable = int(budget_left // s.unit_price)
        if affordable <= 0:
            return None
        if not relax.moq and affordable < s.min_order_quantity:
            return None
        size = min(size, affordable)

    return size or None


# ---- rung 2: production rescheduling -----------------------------------

def _no_delays(inp: SolverInput) -> dict[str, int]:
    return {p.production_order_id: 0 for p in inp.production_orders}


def _minimal_delays(inp: SolverInput) -> dict[str, int] | None:
    """Smallest per-order delay that makes the requirement satisfiable.

    Starts from every order at its max_delay_days, then walks each one back
    toward zero and keeps the smallest delay that stays feasible. High-priority
    orders carry max_delay_days == 0 and so are never candidates — the low
    order absorbs the slip, which is the whole point of the lever.
    """
    delays = {p.production_order_id: p.max_delay_days for p in inp.production_orders}
    if _attempt(inp, delays) is None:
        return None                            # reschedule does not rescue it

    for p in sorted(inp.production_orders, key=lambda p: -p.priority_weight):
        for candidate in range(0, delays[p.production_order_id]):
            trial = dict(delays, **{p.production_order_id: candidate})
            if _attempt(inp, trial) is not None:
                delays[p.production_order_id] = candidate
                break
    return delays


# ---- rung 4: name the constraint that actually bound --------------------

def _diagnose(inp: SolverInput) -> str:
    """Relax one constraint family at a time; report the one that rescues it.

    Person B puts this string straight into the escalation brief, so a judge
    asking "why couldn't it solve this?" gets a real answer, not a shrug.
    """
    delays = _no_delays(inp)
    if inp.allow_reschedule:
        delays = {p.production_order_id: p.max_delay_days
                  for p in inp.production_orders}

    # Order matters when more than one family independently restores
    # feasibility. Report the levers an ops manager can actually pull first;
    # certification is checked last because G3 is a compliance filter nobody
    # is going to relax, and naming it reads as advice to ship uncertified
    # parts. It still surfaces when it is the only thing that binds.
    for name, relax in (
        ("budget", _Relax(budget=True)),
        ("deadline", _Relax(deadline=True)),
        ("available_quantity", _Relax(availability=True, deadline=True)),
        ("moq", _Relax(moq=True)),
        ("certification", _Relax(certification=True)),
    ):
        if relax.availability:
            probe = inp.model_copy(deep=True)
            for s in probe.suppliers:
                s.available_quantity = max(s.available_quantity, 10 ** 6)
            if _attempt(probe, delays, relax=relax) is not None:
                return name
            continue
        if _attempt(inp, delays, relax=relax) is not None:
            return name

    # Nothing single-handedly rescues it: several families bind at once.
    # Report the scarcest resource rather than inventing a winner.
    return "available_quantity"
