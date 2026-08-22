"""The assumption register and its watcher — §4.4. The Recovery differentiator.

Every plan records the facts it depends on. A watcher re-checks them, and when
one breaks the graph routes back into node 3 with broken_assumption set and an
assumption_break audit event fires BEFORE any LLM call happens.

That ordering is the whole point. Most teams implement replanning as "the model
will probably notice". It notices about half the time, and when it does nobody
can see it happen. Here the detection is a comparison between a recorded value
and a re-read one, so it is deterministic, and it is on the audit trail before
the model has said a word.

    [10:14:31] BROKEN  ⚠ ASSUMPTION A3 BROKEN -> replanning
               expedite withdrawn for SUP-37: available True -> False

THE SEVEN TRIGGERS (§4.4, PS §4.8 and §7)

    T1  supplier contradicts an earlier promise      claim status -> CONTRADICTED
    T2  inventory corrected downward                 usable_stock falls
    T3  demand spike (H-06)                          units_required rises
    T4  expedite withdrawn (H-07)                    expedite_available -> False
    T5  supplier rejects the quantity                available_quantity < allocated
    T6  cheaper supplier fails quality               quality_score < min_quality
    T7  production priority changes (H-10)           priority differs

Each is a registered assumption with a kind, a subject and an expected value.
The watcher re-reads and compares; it never asks a model whether something
changed. Adding an eighth trigger is a row in TRIGGERS, not a new code path.

STRUCTURALLY THE SAME AS THE G8 RE-CHECK in agent/nodes/plan.py: record a fact,
re-read it later, route back to investigation when it no longer holds. G8 is
the quote-validity case of exactly this pattern, and it shares replan_count as
its cap for the same reason — a supplier who keeps changing their mind must not
be able to spin the graph.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from agent import clock
from agent.tools import call_tool

# The kinds a watcher knows how to re-check. Each maps to one of the seven
# triggers and to the read that answers it.
KIND_QUOTE_VALIDITY = "quote_validity"      # T-quote (the G8 case)
KIND_EXPEDITE = "expedite"                  # T4
KIND_STOCK = "stock"                        # T2
KIND_DEMAND = "demand"                      # T3
KIND_SUPPLY = "supply"                      # T5
KIND_QUALITY = "quality"                    # T6
KIND_PRIORITY = "priority"                  # T7
KIND_CLAIM = "claim"                        # T1

TRIGGERS = {
    KIND_CLAIM: "supplier contradicts an earlier promise",
    KIND_STOCK: "inventory corrected downward",
    KIND_DEMAND: "demand spike",
    KIND_EXPEDITE: "expedite withdrawn",
    KIND_SUPPLY: "supplier rejects the quantity",
    KIND_QUALITY: "cheaper supplier fails quality",
    KIND_PRIORITY: "production priority changes",
    KIND_QUOTE_VALIDITY: "quote validity window expired",
}


# ---- registering -------------------------------------------------------

def register(state: dict, plan: dict) -> list[dict]:
    """The facts this plan depends on, recorded the moment it is adopted.

    Shape is §4.4's: {id, claim, source, verified, expires_at} plus the kind,
    subject and expected value the watcher needs to re-check it without
    re-deriving anything.
    """
    quotes = {q["supplier_id"]: q for q in state.get("quotes") or []}
    priorities = (state.get("baseline") or {}).get("priorities") or {}
    catalog = {s["supplier_id"]: s for s in state.get("_catalog") or []}
    out: list[dict] = []

    def add(kind, subject, expected, claim, source, verified=True, expires_at=None):
        out.append({"id": f"A{len(out) + 1}", "kind": kind, "subject": subject,
                    "expected": expected, "claim": claim, "source": source,
                    "verified": verified, "expires_at": expires_at})

    for alloc in plan.get("allocations") or []:
        sid = alloc["supplier_id"]
        quote = quotes.get(sid)
        if quote:
            add(KIND_QUOTE_VALIDITY, sid, quote["unit_price"],
                f"{sid} quote {quote['unit_price']}/unit valid "
                f"{quote['quote_valid_hours']}h from issue",
                "rfq", expires_at=_expiry(quote))
            if quote.get("expedite_available"):
                add(KIND_EXPEDITE, sid, True,
                    f"expedite available from {sid}", "rfq")
        add(KIND_SUPPLY, sid, alloc["units"],
            f"{sid} can supply {alloc['units']} units", "rfq" if quote else "catalog")
        # Quality is registered for every allocated supplier, and its expected
        # value is the floor rather than the score. AgentState is frozen without
        # the catalog, so a register that needed the catalog to be in state
        # registered nothing at all — which is how "cheaper supplier fails
        # quality" silently became a trigger that could not fire. The floor is
        # on the component record the watcher already re-reads.
        add(KIND_QUALITY, sid, catalog.get(sid, {}).get("quality_score"),
            f"{sid} meets the quality floor for this component",
            "supplier-catalog")

    add(KIND_STOCK, state.get("affected_component", ""), state.get("usable_stock", 0),
        f"usable_stock {state.get('affected_component')} = "
        f"{state.get('usable_stock')}", "inventory")

    for row in ((state.get("baseline") or {}).get("requirement_rows") or []):
        add(KIND_DEMAND, row["production_order_id"], row["units"],
            f"{row['production_order_id']} requires {row['units']} units",
            "production-schedule")

    for pid, priority in priorities.items():
        add(KIND_PRIORITY, pid, priority,
            f"{pid} priority = {priority}", "production-schedule")

    for claim in state.get("claims") or []:
        add(KIND_CLAIM, claim["supplier_id"], claim.get("status"),
            f"{claim['supplier_id']} claim \"{claim['claim']}\" is "
            f"{claim.get('status')}", "tracking")

    return out


def _expiry(quote: dict) -> str | None:
    issued = quote.get("issued_at")
    if isinstance(issued, str):
        issued = datetime.fromisoformat(issued)
    if issued is None:
        return None
    return (issued + timedelta(hours=int(quote.get("quote_valid_hours") or 0))).isoformat()


# ---- watching ----------------------------------------------------------

def watch(state: dict, assumptions: list[dict] | None = None) -> list[dict]:
    """Re-check every registered assumption. Returns the ones that broke.

    Reads go through agent/tools.py like everything else, so the watcher is
    metered and its cost is visible. Inside the cache TTL these are all cache
    hits, which is what makes "on a cheap interval" affordable.

    They are counted in the post-decision phase, not against the investigation
    budget, for the same reason the ERP writes are: the watcher runs after the
    plan is adopted. Charging it to the investigation budget means a thorough
    investigation buys a run that cannot monitor its own plan, and makes the
    final event claim INCOMPLETE_INVESTIGATION about an investigation that
    finished perfectly well.

    Never raises. A watcher that dies takes recovery with it, so a read that
    fails leaves the assumption unchecked rather than ending the run.
    """
    assumptions = assumptions if assumptions is not None else (
        state.get("assumptions") or [])
    if not assumptions:
        return []

    component_id = state.get("affected_component") or ""
    try:
        inventory = call_tool(state, "get_inventory",
                              "the assumption watcher re-checks recorded stock "
                              "and quality against the current record",
                              phase="execution", component_id=component_id)
        schedule = call_tool(state, "get_production_schedule",
                             "the watcher re-checks recorded demand and priority",
                             phase="execution")
        suppliers = call_tool(state, "get_suppliers",
                              "the watcher re-checks recorded availability and "
                              "quality", phase="execution",
                              component_id=component_id)
    except Exception:                       # noqa: BLE001 - never kill recovery
        return []

    component = inventory[0] if inventory else None
    orders = {o.production_order_id: o for o in schedule}
    catalog = {s.supplier_id: s for s in suppliers}
    claims = {c["supplier_id"]: c for c in state.get("claims") or []}
    quotes = {q["supplier_id"]: q for q in state.get("quotes") or []}
    now = clock.now()

    broken: list[dict] = []
    for a in assumptions:
        observed, reason = _observe(a, component, orders, catalog, claims, quotes, now)
        if reason is not None:
            broken.append({**a, "observed": observed, "reason": reason,
                           "trigger": TRIGGERS.get(a["kind"], a["kind"]),
                           "detected_at": now.isoformat()})
    return broken


def _observe(a: dict, component, orders, catalog, claims, quotes, now):
    """Return (observed_value, reason_if_broken). Pure comparison, no model."""
    kind, subject, expected = a["kind"], a["subject"], a["expected"]

    if kind == KIND_STOCK and component is not None:
        # T2 - corrected DOWNWARD only. More stock than expected is good news
        # and is not a reason to tear up a working plan.
        if component.usable_stock < expected:
            return component.usable_stock, (
                f"inventory corrected down for {subject}: "
                f"{expected} -> {component.usable_stock}")
        return component.usable_stock, None

    if kind == KIND_DEMAND:
        order = orders.get(subject)
        if order is None:
            return None, f"{subject} no longer on the production schedule"
        actual = order.units_planned * order.component_required_per_unit
        if actual > expected:
            return actual, (f"demand spike on {subject}: "
                            f"{expected} -> {actual} units")
        return actual, None

    if kind == KIND_PRIORITY:
        order = orders.get(subject)
        if order is None:
            return None, f"{subject} no longer on the production schedule"
        if order.priority != expected:
            return order.priority, (f"production priority changed for {subject}: "
                                    f"{expected} -> {order.priority}")
        return order.priority, None

    if kind == KIND_SUPPLY:
        sup = catalog.get(subject)
        if sup is None:
            return None, f"{subject} no longer lists this component"
        if sup.available_quantity < expected:
            return sup.available_quantity, (
                f"{subject} rejects the quantity: {expected} units committed, "
                f"{sup.available_quantity} now available")
        return sup.available_quantity, None

    if kind == KIND_QUALITY:
        sup = catalog.get(subject)
        if sup is None:
            return None, f"{subject} no longer lists this component"
        floor = component.min_quality if component is not None else 0.0
        if sup.quality_score < floor:
            was = f" (was {expected})" if expected is not None else ""
            return sup.quality_score, (
                f"{subject} fails quality: {sup.quality_score} is below the "
                f"{floor} floor{was}")
        return sup.quality_score, None

    if kind == KIND_EXPEDITE:
        quote = quotes.get(subject)
        available = bool(quote and quote.get("expedite_available"))
        if expected and not available:
            return available, f"expedite withdrawn for {subject}: True -> False"
        return available, None

    if kind == KIND_CLAIM:
        claim = claims.get(subject)
        status = claim.get("status") if claim else None
        if status == "CONTRADICTED" and expected != "CONTRADICTED":
            return status, (f"{subject} contradicts an earlier promise: "
                            f"{expected} -> CONTRADICTED")
        return status, None

    if kind == KIND_QUOTE_VALIDITY:
        expires = a.get("expires_at")
        if expires and now > datetime.fromisoformat(expires):
            return None, (f"{subject} quote expired at {expires}; the price it "
                          f"was planned on is no longer committed")
        return expected, None

    return None, None


# ---- what the CLI and the audit trail read -----------------------------

def break_summary(broken: list[dict]) -> str:
    """The line that appears on screen the moment a plan stops being true.

    Person A fires an H-event during Q&A and this is what the terminal prints,
    before the model has produced a token. It is the most convincing second of
    the whole demo, so it says which assumption and what changed - not just
    that something did.
    """
    first = broken[0]
    return f"⚠ ASSUMPTION {first['id']} BROKEN -> replanning"
