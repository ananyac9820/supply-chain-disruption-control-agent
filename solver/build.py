"""SolverInput from live sandbox state — a reference implementation.

Track B owns node 4 and may build SolverInput its own way; this exists
because the demo harness needs it, and because two things are easy to get
wrong here and expensive to find at hour 14:

  * G3 and G4 are pre-solve filters (§4.4). A supplier that fails either is
    removed from the candidate set, not passed through with a note. The
    solver double-checks both as C3 and C4, but the filtering belongs here.
  * effective_reliability, not reliability_score, feeds the risk term. Pass
    the catalog score and reputation stops changing any decision — which is
    the failure mode §4.5 warns about, and it is silent.

The `contradicted` argument keeps its contract name because
SolverSupplier.claim_contradicted is frozen, but read it as *unconfirmed*:
the supplier is out of this plan because its units cannot be verified, which
is not the same as a finding against the supplier. unconfirmed_shipment_suppliers()
derives the set from shipment confidence.

Take it or replace it; if you replace it, keep both of those.
"""

from datetime import date, datetime

from contracts.constants import MAX_DELAY_DAYS, PRIORITY_WEIGHT
from contracts.models import SolverInput, SolverProdOrder, SolverSupplier
from trust import effective_reliability, units_confirmed


def build_solver_input(
    client,
    component_id: str,
    *,
    budget_cap: float,
    now: datetime | date | None = None,
    contradicted: set[str] | None = None,
    expired_quotes: set[str] | None = None,
    allow_reschedule: bool = True,
    allow_partial: bool = False,
) -> SolverInput:
    """Read the world, apply the pre-solve filters, return a SolverInput."""
    contradicted = contradicted or set()
    expired_quotes = expired_quotes or set()
    today = _as_date(now) if now is not None else _as_date(client.sim_clock()["now"])

    component = client.get_inventory(component_id)[0]
    required = set(component.required_certifications)

    suppliers = []
    for s in client.get_suppliers(component_id):
        certified = required <= set(s.certifications)
        if not certified or s.quality_score < component.min_quality:
            continue                      # G3 / G4 — removed, not annotated
        suppliers.append(SolverSupplier(
            supplier_id=s.supplier_id,
            unit_price=s.unit_price,
            lead_time_days=s.lead_time_days,
            available_quantity=s.available_quantity,
            min_order_quantity=s.min_order_quantity,
            effective_reliability=effective_reliability(
                s.supplier_id, s.reliability_score),
            certified=certified,
            quality_score=s.quality_score,
            quote_expired=s.supplier_id in expired_quotes,
            claim_contradicted=s.supplier_id in contradicted,
        ))

    orders = [
        SolverProdOrder(
            production_order_id=p.production_order_id,
            units_required=p.units_planned * p.component_required_per_unit,
            deadline_day=(p.deadline - today).days,
            priority_weight=PRIORITY_WEIGHT[p.priority],
            max_delay_days=p.max_delay_days,
        )
        for p in client.get_production_schedule()
        if p.required_component == component_id
    ]

    return SolverInput(
        component_id=component_id,
        usable_stock=component.usable_stock,      # never current_stock
        safety_stock=component.safety_stock,
        daily_usage=component.daily_usage,
        min_quality=component.min_quality,
        suppliers=suppliers,
        production_orders=orders,
        budget_cap=budget_cap,
        allow_reschedule=allow_reschedule,
        allow_partial=allow_partial,
    )


def baseline(client, component_id: str, now: datetime | date | None = None) -> dict:
    """What happens if we do nothing — the denominator for every plan (D-5).

    Cost Control is 20% of the rubric and "did it avoid unnecessary spending"
    is unjudgeable without this number.
    """
    today = _as_date(now) if now is not None else _as_date(client.sim_clock()["now"])
    component = client.get_inventory(component_id)[0]
    on_hand = component.usable_stock - component.safety_stock

    orders = sorted(
        (p for p in client.get_production_schedule()
         if p.required_component == component_id),
        key=lambda p: p.deadline)

    remaining = on_hand
    units_short = 0
    misses = []
    for p in orders:
        need = p.units_planned * p.component_required_per_unit
        covered = max(0, min(need, remaining))
        remaining -= covered
        if covered < need:
            units_short += need - covered
            misses.append(p.production_order_id)

    return {
        "coverage_days": round(component.usable_stock / component.daily_usage, 2)
        if component.daily_usage else None,
        "units_short": units_short,
        "deadline_misses": misses,
        "production_days_lost": round(units_short / component.daily_usage, 2)
        if component.daily_usage else None,
        "at_risk_orders": misses,
    }


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(value).date()


def unconfirmed_shipment_suppliers(client) -> set[str]:
    """Suppliers whose in-transit units cannot be counted, by PO confidence.

    Reads the fast axis only. A supplier lands here because one of its
    shipments is unverifiable — including when the evidence points at the
    courier, or at nobody — and not because its reputation has moved.
    """
    return {po.supplier_id for po in client.get_purchase_orders()
            if not units_confirmed(po.po_id)}
