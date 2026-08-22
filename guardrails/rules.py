"""The deterministic post-checks, G1-G12 (§4.4).

Six rules live here. The other six do not, and that is the design:

    G3  certification        pre-solve filter   (solver/fallback._eligible)
    G4  quality floor        pre-solve filter   (solver/fallback._eligible)
    G6  MOQ                  model constraint   (solver/fallback._order_size)
    G7  availability         model constraint   (solver, x[s] <= avail[s])
    G11 arrival vs deadline  model constraint   (solver, per-deadline coverage)

A plan violating any of those cannot be constructed, so re-checking them here
would be checking the solver's arithmetic against itself. If one of them ever
fires post-solve, the bug is in the model, not in the plan — which is why
`unreachable_pre_solve_rules` exists below and is asserted in the tests.

Each rule takes (plan, context) and returns a Finding or None. A rule that
fires does one of two things, and the distinction matters to node 4:

    veto=True   the plan is invalid. Re-solve. (§7 F-8: max two rounds.)
    veto=False  the plan is valid but may not execute autonomously.
                Escalate. Re-solving will produce the same plan.

G2 is the second kind. A plan over the approval threshold is not wrong — it
needs a human. Sending node 4 back to the solver for it would burn a
correction round and return the identical plan.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from contracts.constants import APPROVAL_THRESHOLD
from contracts.models import SolverOutput

PRE_SOLVE_RULES = ("G3", "G4", "G6", "G7", "G11")
POST_CHECK_RULES = ("G1", "G2", "G5", "G8", "G9", "G12")


@dataclass(frozen=True)
class Finding:
    rule: str
    reason: str
    veto: bool = True
    forced_escalation: bool = False


def _now(context: dict) -> datetime:
    value = context.get("now")
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return datetime.now()


def g1_budget(plan: SolverOutput, context: dict) -> Finding | None:
    """Plan cost exceeds the budget still available. Re-solve under a tighter cap."""
    remaining = context.get("remaining_budget")
    if remaining is None or plan.total_cost <= remaining:
        return None
    return Finding(
        "G1",
        f"plan costs {plan.total_cost:,.2f} against {remaining:,.2f} remaining "
        f"budget — over by {plan.total_cost - remaining:,.2f}",
    )


def g2_approval(plan: SolverOutput, context: dict) -> Finding | None:
    """Above the approval threshold, execution is blocked pending a human.

    Strictly greater-than, matching PS §5.8 and /approval/check: a plan
    costing exactly the threshold executes autonomously.
    """
    limit = context.get("approval_limit", float(APPROVAL_THRESHOLD))
    if plan.total_cost <= limit:
        return None
    return Finding(
        "G2",
        f"plan costs {plan.total_cost:,.2f}, exceeding the autonomous purchase "
        f"threshold of {limit:,.0f} by {plan.total_cost - limit:,.2f}",
        veto=False,               # valid plan, needs a human — do not re-solve
        forced_escalation=True,
    )


def g5_safety_stock(plan: SolverOutput, context: dict) -> Finding | None:
    """Safety stock is not free inventory.

    Either the plan preserves it, or it carries an explicit justification and
    escalates. Never silently consumed. A breach affecting a high-priority
    order always escalates, justified or not.
    """
    projected = context.get("projected_stock")
    floor = context.get("safety_stock")
    if projected is None or floor is None or projected >= floor:
        return None

    justification = context.get("safety_stock_breach_justification")
    high_priority = "high" in context.get("affected_priorities", [])
    shortfall = floor - projected

    if not justification:
        return Finding(
            "G5",
            f"plan leaves projected stock at {projected} against a safety floor "
            f"of {floor} ({shortfall} short) with no justification recorded",
            veto=True,
            forced_escalation=high_priority,
        )
    return Finding(
        "G5",
        f"safety stock breached by {shortfall} units, justified: {justification}",
        veto=False,
        forced_escalation=high_priority,
    )


def g8_quote_expiry(plan: SolverOutput, context: dict) -> Finding | None:
    """An expired quote invalidates that supplier's allocation. Only that one."""
    now = _now(context)
    quotes = {q["supplier_id"]: q for q in context.get("quotes", [])}
    expired = []
    for allocation in plan.allocations:
        quote = quotes.get(allocation.supplier_id)
        if quote is None:
            continue
        issued = quote["issued_at"]
        if isinstance(issued, str):
            issued = datetime.fromisoformat(issued)
        if now > issued + timedelta(hours=quote.get("quote_valid_hours", 0)):
            expired.append(allocation.supplier_id)
    if not expired:
        return None
    return Finding(
        "G8",
        f"quote expired for {', '.join(sorted(expired))}; re-RFQ that supplier "
        f"only, the rest of the plan stands",
    )


# Below this, a shipment's units are not counted as confirmed. Mirrors
# trust.UNCONFIRMED_BELOW; a test asserts the two agree. guardrails/ stays
# free of any dependency on the ledger or the database.
DEFAULT_UNCONFIRMED_BELOW = 0.5


def g9_unconfirmed_shipment(plan: SolverOutput, context: dict) -> Finding | None:
    """Units we cannot confirm may not be counted on.

    This keys on shipment confidence, not on reputation. The units are
    excluded because the evidence does not establish that they exist and are
    moving — not because anyone has concluded the supplier acted in bad faith.
    A shipment can be unverifiable through a courier's failure, or through
    nobody's failure at all, and the plan has to treat it the same way in
    every case: as units it cannot count.

    Nothing here is a finding about the supplier. Attribution is recorded
    separately, on the incident, and only an incident attributed to the
    supplier reaches reputation.
    """
    threshold = context.get("unconfirmed_below", DEFAULT_UNCONFIRMED_BELOW)
    confidence = dict(context.get("shipment_confidence", {}))

    # Legacy input: a caller still supplying claims gets the same protection.
    for claim in context.get("claims", []):
        if claim.get("status") == "CONTRADICTED":
            confidence.setdefault(claim["supplier_id"], 0.0)

    unconfirmed = sorted(
        a.supplier_id for a in plan.allocations
        if confidence.get(a.supplier_id, 1.0) < threshold)
    if not unconfirmed:
        return None
    return Finding(
        "G9",
        f"claim inconsistent with tracking evidence for "
        f"{', '.join(unconfirmed)}; units unconfirmed and not counted toward "
        f"coverage",
    )


def g12_infeasible(plan: SolverOutput, context: dict) -> Finding | None:
    """No feasible plan after the full ladder. Escalate, naming what bound."""
    if plan.status != "INFEASIBLE":
        return None
    binding = plan.binding_constraint or "unknown"
    return Finding(
        "G12",
        f"no feasible plan after the full relaxation ladder; binding "
        f"constraint: {binding}",
        veto=False,                # re-solving cannot help — the ladder ran out
        forced_escalation=True,
    )


POST_CHECKS = (g1_budget, g2_approval, g5_safety_stock, g8_quote_expiry,
               g9_unconfirmed_shipment, g12_infeasible)
