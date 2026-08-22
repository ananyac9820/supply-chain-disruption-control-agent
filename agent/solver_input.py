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
from agent.integrations import UNCONFIRMED_BELOW, reliability_of
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


def commitments_from(erp_writes: list[dict]) -> list[dict]:
    """Units already bought, read back off the ERP writes node 6 made.

    §4.5: on a replan we re-solve for the shortfall ONLY. Units already
    confirmed from other suppliers stay committed — tearing up a good purchase
    order because a different supplier moved is how a replan costs more than
    the disruption did.
    """
    out = []
    for write in erp_writes or []:
        if write.get("action") != "create_alternate_po":
            continue
        p = write.get("payload") or {}
        out.append({"supplier_id": p.get("supplier_id"),
                    "units": int(p.get("quantity") or 0),
                    "arrival_day": int(p.get("arrival_day") or 0)})
    return out


def apply_commitments(orders: list[SolverProdOrder],
                      suppliers: list[SolverSupplier],
                      commitments: list[dict]) -> tuple[list, list]:
    """Net the committed units out of both sides of the problem.

    Off the demand side: a committed unit arriving by an order's deadline
    already covers part of it, so that order needs less. Applied in deadline
    order, the same walk node 2 uses, so the two never disagree.

    Off the supply side: a supplier who has already sold us 460 units has 460
    fewer left, so the solver cannot spend them twice.
    """
    if not commitments:
        return orders, suppliers

    # Work on copies: the caller's commitment list is state and must survive
    # this function unchanged, while the walk below consumes as it goes.
    pool = sorted(({"supplier_id": c["supplier_id"], "units": c["units"],
                    "arrival_day": c["arrival_day"]} for c in commitments),
                  key=lambda c: c["arrival_day"])

    netted: list[SolverProdOrder] = []
    # Ordered by how late each order CAN run, not by when it is due. Committed
    # units are scarce, and the order with no slack must have first claim on the
    # early arrivals. Walking by raw deadline lets a flexible low-priority order
    # take the day-4 delivery that the immovable high-priority one needed, and
    # the re-solve then reports INFEASIBLE on supply that was merely misassigned.
    for order in sorted(orders, key=lambda o: (o.deadline_day + o.max_delay_days,
                                               o.deadline_day)):
        need = order.units_required
        # The LATEST this order could run, not its original date: the solver is
        # still free to reschedule it. Matching on the original deadline strands
        # commitments that arrive later while their availability has already
        # been deducted, and the re-solve then reports INFEASIBLE on supply it
        # actually owns.
        latest = order.deadline_day + order.max_delay_days
        for c in pool:
            if need <= 0:
                break
            if c["arrival_day"] > latest or c["units"] <= 0:
                continue
            used = min(need, c["units"])
            need -= used
            c["units"] -= used
        netted.append(order.model_copy(update={"units_required": need}))

    bought: dict[str, int] = {}
    for c in commitments:                       # the originals, not the pool
        bought[c["supplier_id"]] = bought.get(c["supplier_id"], 0) + c["units"]
    trimmed = [s.model_copy(update={
        "available_quantity": max(0, s.available_quantity
                                  - bought.get(s.supplier_id, 0))})
        for s in suppliers]
    return netted, trimmed


def _unconfirmed(claim: dict) -> bool:
    """Whether this claim leaves its supplier's units uncountable.

    Prefers the recorded shipment confidence; falls back to the claim tag for a
    claim recorded before the two axes existed.
    """
    confidence = claim.get("shipment_confidence")
    if confidence is not None:
        return float(confidence) < UNCONFIRMED_BELOW
    if claim.get("units_confirmed") is False:
        return True
    return claim.get("status") == "CONTRADICTED"


def confidence_map(claims: list[dict]) -> dict[str, float]:
    """{supplier_id: shipment_confidence} for guardrails' G9."""
    out: dict[str, float] = {}
    for claim in claims:
        confidence = claim.get("shipment_confidence")
        if confidence is None and _unconfirmed(claim):
            confidence = 0.0
        if confidence is not None:
            sid = claim["supplier_id"]
            out[sid] = min(out.get(sid, 1.0), float(confidence))
    return out


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
    # SolverSupplier.claim_contradicted is a frozen field name, and its meaning
    # is now the shipment axis: "the units from this supplier cannot be
    # confirmed", not "this supplier acted in bad faith". A claim tagged
    # CONTRADICTED is what triggers the check, but what fills the flag is the
    # shipment confidence that resulted — which is also what guardrails' G9
    # keys on, so the pre-solve filter and the post-check agree.
    contradicted = set() if ignore_contradictions else {
        c["supplier_id"] for c in claims if _unconfirmed(c)}
    by_quote = {q["supplier_id"]: q for q in quotes}

    eligible: list[SolverSupplier] = []
    rejected: list[dict] = []

    # The latest day any order could still run, allowing for every reschedule
    # the solver is permitted. A supplier that cannot deliver by then cannot
    # contribute to any plan, so it is a hard elimination like certification -
    # and naming it is what shows the brief's first section is a filter and not
    # a low weighting.
    horizon = max((days_from(now, o.deadline) + o.max_delay_days
                   for o in production_orders
                   if o.required_component == component.component_id),
                  default=None)

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
        lead = int(quote["delivery_days"]) if quote else sup.lead_time_days

        # EVERY hard constraint a supplier fails is recorded, not just the
        # first. SUP-18 is both uncertified AND under the quality floor, and
        # the brief's whole argument is that eligibility is a filter rather
        # than a weighting - a filter that reported one reason and stopped
        # would understate how far outside the candidate set it sits.
        failures: list[tuple[str, str]] = []
        if not is_certified:
            failures.append(("G3", f"missing "
                             f"{', '.join(missing_certifications(sup, component))} "
                             f"certification"))
        if not quality_ok:
            failures.append(("G4", f"quality {sup.quality_score:.2f} below floor "
                                   f"{component.min_quality:.2f}"))
        if horizon is not None and lead > horizon:
            failures.append(("G11", f"lead time {lead}d cannot meet the latest "
                                    f"deadline (day {horizon}) even fully "
                                    f"rescheduled"))
        drop = bool(failures)

        # Not eliminations. The supplier remains a candidate on its merits; the
        # solver's own filters decide what to do with the flag.
        if not drop and is_contradicted:
            confidence = next(
                (c.get("shipment_confidence") for c in claims
                 if c["supplier_id"] == sup.supplier_id
                 and c.get("shipment_confidence") is not None), None)
            failures.append(("G9",
                             f"shipment confidence {confidence:.2f} - units "
                             f"unconfirmed pending evidence"
                             if confidence is not None
                             else "units unconfirmed pending evidence"))
        if not drop and expired:
            failures.append(("G8", f"quote expired after "
                                   f"{quote['quote_valid_hours']}h"))

        for rule, reason in failures:
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
            effective_reliability=reliability_of(sup.supplier_id, sup.reliability_score),
            certified=is_certified,
            quality_score=sup.quality_score,
            quote_expired=expired,
            # NAME vs MEANING. This field is frozen in contracts/models.py and
            # its name no longer describes what it carries. It does NOT mean
            # "this supplier made a false claim". It means "the units from this
            # supplier cannot be confirmed", and it is fed from the shipment
            # confidence axis - the same value guardrails' G9 keys on, so the
            # pre-solve filter and the post-check cannot disagree.
            #
            # A discrepancy between a claim and tracking drops shipment
            # confidence whoever turns out to be responsible; only an incident
            # the evidence attributes to SUPPLIER moves reputation, and
            # reputation reaches the solver through effective_reliability
            # above, not through this flag. On the COMP-104 scenario this is
            # True for SUP-21 while its reputation is untouched at 0.72.
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
        # G9 keys on the shipment axis now. claims stays for their documented
        # legacy path, but shipment_confidence is what the rule should read.
        "shipment_confidence": confidence_map(claims),
        "unconfirmed_below": UNCONFIRMED_BELOW,
        "now": now,
    }
