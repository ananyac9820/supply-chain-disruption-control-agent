"""build_solver_input — deterministic assembly of the solver's inputs.

No LLM touches this file. Everything here is a lookup, a filter or a
comparison, and every one of them is the kind of thing that must be the same
number twice or the audit trail contradicts itself.

WHERE NODE 3's WORK LANDS
    claim_contradicted is the field that carries verification into the
    decision. Node 3 checks a dispatch claim against tracking; if it is
    contradicted, that supplier arrives here flagged, the solver's C8 drops it,
    and the plan changes. Without this flag the verification is a log line that
    changed nothing. On the COMP-104 scenario it is worth 8,690 and it is the
    difference between a plan that executes autonomously and one that needs a
    human — the counterfactual in agent/nodes/plan.py measures exactly that.

    effective_reliability carries the same information as a weight rather than
    a switch: it is the catalog score after the trust ledger's penalties, and
    it feeds the solver's w_risk term.

QUALITY AND CERTIFICATION
    min_quality comes off the component record, not a constant. G4 is a
    pre-solve filter and callers are still expected to drop sub-threshold
    suppliers when building this input, but the field is populated too so one
    forgotten line here cannot let an under-quality supplier win.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent.impact_math import days_from, units_required
from agent.integrations import trust_read
from contracts.constants import (APPROVAL_THRESHOLD, EMERGENCY_BUDGET,
                                 PRIORITY_WEIGHT)
from contracts.models import (Component, ProductionOrder, SolverInput,
                              SolverProdOrder, SolverSupplier, Supplier)


def quote_expired(quote: dict, now: datetime) -> bool:
    """A quote is a commitment with a shelf life. Past it, the price is a
    guess again (G8)."""
    issued = quote.get("issued_at")
    if issued is None:
        return False
    if isinstance(issued, str):
        issued = datetime.fromisoformat(issued)
    return now > issued + timedelta(hours=int(quote.get("quote_valid_hours") or 0))


def certified(supplier: Supplier, component: Component) -> bool:
    return set(component.required_certifications) <= set(supplier.certifications)


def missing_certifications(supplier: Supplier, component: Component) -> list[str]:
    return sorted(set(component.required_certifications) - set(supplier.certifications))


def build_solver_input(
    component: Component,
    suppliers: list[Supplier],
    production_orders: list[ProductionOrder],
    quotes: list[dict],
    claims: list[dict],
    now: datetime,
    reserved_budget: float = 0.0,
    allow_reschedule: bool = True,
    allow_partial: bool = False,
    ignore_contradictions: bool = False,
) -> tuple[SolverInput, list[dict]]:
    """Return the SolverInput and the list of suppliers filtered out, with why.

    The second element is what PS §4.10 calls "alternatives considered". It is
    built here because this is the only place that knows both the rule and the
    supplier it removed.

    ignore_contradictions=True builds the counterfactual input: the same world
    as if node 3 had never checked the dispatch claim. Node 4 solves both and
    reports the difference, which is what the verification was worth. It is
    never used for a plan that executes - only to measure one.
    """
    contradicted = set() if ignore_contradictions else {
        c["supplier_id"] for c in claims if c.get("status") == "CONTRADICTED"}
    by_quote = {q["supplier_id"]: q for q in quotes}

    eligible: list[SolverSupplier] = []
    rejected: list[dict] = []

    for sup in suppliers:
        if sup.component_id != component.component_id:
            continue

        is_certified = certified(sup, component)
        quality_ok = sup.quality_score >= component.min_quality
        is_contradicted = sup.supplier_id in contradicted
        quote = by_quote.get(sup.supplier_id)
        expired = quote_expired(quote, now) if quote else False

        # WHO DROPS WHAT, AND WHY IT MATTERS
        #
        # G3 and G4 are dropped here. §4.4 makes the quality floor the
        # caller's job explicitly, and an uncertified supplier should never
        # reach the solver at all.
        #
        # G8 and G9 are NOT dropped here. contracts/models.py gives
        # SolverSupplier a quote_expired and a claim_contradicted field for
        # exactly this, and solver/fallback._eligible filters on both. Dropping
        # the supplier instead of setting its flag would leave those fields
        # permanently False and make the solver's own filter dead code — and it
        # would make guardrails' G8 unreachable, because that rule fires on an
        # allocation whose quote has expired, which is what happens when a plan
        # sits at the approval gate long enough for a quote to go stale.
        #
        # Both still appear in rejected_alternatives: the audit trail wants
        # every supplier that was considered and set aside, whoever set it
        # aside.
        reason, rule, drop = None, None, False
        if not is_certified:
            missing = ", ".join(missing_certifications(sup, component))
            reason, rule, drop = f"missing {missing} certification", "G3", True
        elif not quality_ok:
            reason, rule, drop = (f"quality {sup.quality_score} below the "
                                  f"{component.min_quality} floor"), "G4", True
        elif is_contradicted:
            reason, rule = ("dispatch claim CONTRADICTED by tracking; units "
                            "count as 0 confirmed"), "G9"
        elif expired:
            reason, rule = (f"quote expired after "
                            f"{quote['quote_valid_hours']}h"), "G8"

        if reason is not None:
            rejected.append({
                "supplier_id": sup.supplier_id, "rule": rule, "reason": reason,
                # the phrasing the decision brief prints verbatim
                "label": f"{sup.supplier_id}, rejected: {reason}",
            })
        if drop:
            continue

        # Price from the quote when we have one: a catalog price is not a
        # commitment, and the quote is what the supplier will actually honour.
        unit_price = float(quote["unit_price"]) if quote else sup.unit_price
        lead = int(quote["delivery_days"]) if quote else sup.lead_time_days
        available = (min(sup.available_quantity, int(quote["quantity_available"]))
                     if quote else sup.available_quantity)

        eligible.append(SolverSupplier(
            supplier_id=sup.supplier_id,
            unit_price=unit_price,
            lead_time_days=lead,
            available_quantity=available,
            min_order_quantity=sup.min_order_quantity,
            effective_reliability=trust_read(sup.supplier_id, sup.reliability_score),
            certified=is_certified,
            quality_score=sup.quality_score,
            quote_expired=expired,
            claim_contradicted=is_contradicted,
        ))

    orders = [
        SolverProdOrder(
            production_order_id=o.production_order_id,
            units_required=units_required(o),
            deadline_day=days_from(now, o.deadline),
            priority_weight=PRIORITY_WEIGHT.get(o.priority, 1.0),
            max_delay_days=o.max_delay_days,
        )
        for o in production_orders
        if o.required_component == component.component_id
    ]
    orders.sort(key=lambda o: (o.deadline_day, o.production_order_id))

    inp = SolverInput(
        component_id=component.component_id,
        usable_stock=component.usable_stock,          # never current_stock
        safety_stock=component.safety_stock,
        daily_usage=component.daily_usage,
        suppliers=eligible,
        production_orders=orders,
        min_quality=component.min_quality,
        budget_cap=max(0.0, EMERGENCY_BUDGET - float(reserved_budget or 0.0)),
        approval_limit=float(APPROVAL_THRESHOLD),
        allow_reschedule=allow_reschedule,
        allow_partial=allow_partial,
    )
    return inp, rejected


def validator_context(inp: SolverInput, plan, quotes: list[dict],
                      claims: list[dict], now: datetime,
                      affected_priorities: list[str],
                      justification: str | None = None) -> dict:
    """The context dict guardrails/validator.py documents.

    Keys are theirs, not ours - approval_limit in particular is a frozen name.
    Every key except approval_limit is optional there: a rule whose inputs are
    absent does not fire, so an incomplete context yields fewer findings rather
    than a crash.
    """
    bought = sum(a.units for a in plan.allocations) if plan else 0
    projected = inp.usable_stock + bought - sum(
        o.units_required for o in inp.production_orders)
    return {
        "approval_limit": inp.approval_limit,
        "remaining_budget": inp.budget_cap,
        "projected_stock": projected,
        "safety_stock": inp.safety_stock,
        "affected_priorities": affected_priorities,
        "safety_stock_breach_justification": justification,
        "quotes": quotes,
        "claims": claims,
        "now": now,
    }
