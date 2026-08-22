"""Deterministic impact arithmetic. No LLM touches anything in this file.

Two numbers that are easy to get wrong, and both are graded:

USABLE STOCK, NEVER CURRENT STOCK
    coverage_days = usable_stock / daily_usage      # 390 / 90 = 4.33
    The ERP header says 420. Reasoning from it is how PS Scenario 2 / H-02 is
    quietly failed.

CONTINUITY IS CUMULATIVE
    Production orders are walked in deadline order and each consumes what the
    earlier ones left. The requirement at the second order's deadline is the
    running total, not that order's own units. Reasoning per-order understates
    the gap and puts these numbers at odds with the solver's C6, which is the
    one disagreement that makes an audit trail indefensible: node 2 would
    report a shortfall the solver never planned for.

    COMP-104 worked example, at usable 390 / safety 150:
        free of safety stock                          240
        PROD-914  day 2   700 units   cumulative  700   short  460
        PROD-882  day 4   700 units   cumulative 1400   short 1160

COVERAGE VS REQUIREMENT
    These measure different things and must not be added together.
    coverage_days is the headline burn-down at daily_usage - "how long until
    the line stops at the current rate". The cumulative requirement is the
    specific demand the solver plans against. daily_usage does not appear in
    the solver's C6, so it does not appear in the shortfall either.
"""

from __future__ import annotations

from datetime import date, datetime

from contracts.constants import PRIORITY_WEIGHT, W_LATE
from contracts.models import Component, ProductionOrder

# A Track B assumption: contracts/constants.py carries no horizon, and the
# counterfactual needs one to be finite. Fourteen days is the simulation
# horizon from the build plan. One constant, so correcting it is one line.
PLANNING_HORIZON_DAYS = 14


def coverage_days(usable_stock: int, daily_usage: int) -> float:
    """Days of cover at the current burn rate. From usable_stock, always."""
    if daily_usage <= 0:
        return float("inf")
    return round(usable_stock / daily_usage, 2)


def days_from(now: datetime | date, deadline: date) -> int:
    today = now.date() if isinstance(now, datetime) else now
    return (deadline - today).days


def units_required(order: ProductionOrder) -> int:
    return order.units_planned * order.component_required_per_unit


def requirement_walk(component: Component, orders: list[ProductionOrder],
                     now: datetime | date) -> list[dict]:
    """Walk the orders for this component in deadline order, each consuming
    what earlier ones left. Returns one row per order, in that order."""
    relevant = [o for o in orders if o.required_component == component.component_id]
    relevant.sort(key=lambda o: (o.deadline, o.production_order_id))

    free = max(0, component.usable_stock - component.safety_stock)
    remaining = free
    cumulative = 0
    rows: list[dict] = []

    for order in relevant:
        need = units_required(order)
        cumulative += need
        consumed = min(remaining, need)
        remaining -= consumed
        rows.append({
            "production_order_id": order.production_order_id,
            "priority": order.priority,
            "deadline": order.deadline.isoformat(),
            "deadline_day": days_from(now, order.deadline),
            "units": need,
            "cumulative": cumulative,
            "covered_from_stock": consumed,
            "shortfall": need - consumed,                 # this order's own gap
            "cumulative_shortfall": max(0, cumulative - free),
            "max_delay_days": order.max_delay_days,
        })
    return rows


def at_risk(rows: list[dict]) -> list[str]:
    """Orders that on-hand stock cannot cover by their deadline."""
    return [r["production_order_id"] for r in rows if r["shortfall"] > 0]


def baseline(component: Component, rows: list[dict],
             horizon_days: int = PLANNING_HORIZON_DAYS) -> dict:
    """The counterfactual: what happens if the agent does nothing.

    Every plan is later reported as a delta against this, which is what gives
    Cost Control a denominator - "12% over baseline" means something, "cost
    168,000" does not. PS §8 Scenario 5 also requires the escalation brief to
    state the risk of no action, and this is where that number comes from.

    cost_of_inaction weights each missed order by how late it would run to the
    end of the horizon, priority-weighted at W_LATE per priority-weighted day -
    the same currency the solver minimises in, so plan and baseline are
    comparable rather than merely adjacent.
    """
    free = max(0, component.usable_stock - component.safety_stock)
    total_required = rows[-1]["cumulative"] if rows else 0
    units_short = max(0, total_required - free)

    misses = [r for r in rows if r["shortfall"] > 0]
    cost = 0.0
    for r in misses:
        days_late = max(0, horizon_days - r["deadline_day"])
        cost += W_LATE * PRIORITY_WEIGHT.get(r["priority"], 1.0) * days_late

    daily = component.daily_usage or 1
    return {
        "units_short": units_short,
        "production_days_lost": round(units_short / daily, 2),
        "deadline_misses": [r["production_order_id"] for r in misses],
        "cost_of_inaction": round(cost, 2),
        "horizon_days": horizon_days,
        "free_of_safety_stock": free,
        "total_required": total_required,
    }
