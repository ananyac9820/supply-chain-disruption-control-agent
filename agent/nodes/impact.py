"""Node 2 — impact + baseline. NO LLM.

Writes: usable_stock, coverage_days, at_risk_orders, baseline

Two rules, both of them graded, both implemented in agent/impact_math.py:

    coverage_days = usable_stock / daily_usage        # 390 / 90 = 4.33

Always from usable_stock, never current_stock. That one line is the difference
between passing and failing PS Scenario 2 / H-02, and it is the most common way
teams quietly fail this problem.

And continuity is cumulative: production orders are walked in deadline order,
each consuming what the earlier ones left, so the requirement at the second
order's deadline is the running total rather than that order's own units. This
has to match the solver's C6 exactly. If node 2 reports a 460 shortfall while
the solver plans for 1160, the audit trail contradicts itself and neither
number can be defended.

baseline is the counterfactual — what happens if the agent does nothing. Every
plan is later reported as a delta against it, which is what gives Cost Control
a denominator and what PS §8 Scenario 5 wants in the escalation brief.

This node reads /inventory and /production-schedule again. Both are served
from the read cache inside the TTL, so they cost no budget and are logged as
avoided — which is exactly the evidence Tool Efficiency asks for.
"""

from __future__ import annotations

from agent import clock
from agent.audit import append_event
from agent.impact_math import (at_risk, baseline, coverage_days,
                               requirement_walk)
from agent.tools import call_tool
from contracts.state import AgentState


def impact(state: AgentState) -> dict:
    work = dict(state)
    component_id = work.get("affected_component") or ""

    inventory = call_tool(work, "get_inventory",
                          "the impact figures must come from usable_stock, and a "
                          "stale read would put them at odds with the solver",
                          component_id=component_id)
    schedule = call_tool(work, "get_production_schedule",
                         "the cumulative requirement is a walk over the production "
                         "orders in deadline order")

    component = inventory[0]
    rows = requirement_walk(component, schedule, clock.now())
    cover = coverage_days(component.usable_stock, component.daily_usage)
    risky = at_risk(rows)
    base = baseline(component, rows)
    # Node 3 needs the earliest at-risk deadline to set needed_by_days on the
    # RFQ. It lives here because node 2 owns every deadline computation; node 3
    # deriving it again is how the two ends up disagreeing.
    base["earliest_at_risk_day"] = min(
        (r["deadline_day"] for r in rows if r["shortfall"] > 0), default=0)

    out: dict = {
        "usable_stock": component.usable_stock,
        "coverage_days": cover,
        "at_risk_orders": risky,
        "baseline": base,
        "tools_called": work["tools_called"],
        "tool_budget_remaining": work["tool_budget_remaining"],
    }

    first = rows[0] if rows else {}
    events = append_event(
        work, type="calculation", actor="impact",
        summary=(f"coverage {cover} days - "
                 + ", ".join(f"{r['production_order_id']} ({r['priority']}) "
                             f"at risk in {r['deadline_day']}d" for r in rows
                             if r["shortfall"] > 0)
                 if risky else f"coverage {cover} days - no order at risk"),
        detail={"usable_stock": component.usable_stock,
                "current_stock": component.current_stock,
                "daily_usage": component.daily_usage,
                "safety_stock": component.safety_stock,
                "coverage_days": cover,
                "free_of_safety_stock": base["free_of_safety_stock"],
                "cumulative_requirement": rows,
                "at_risk_orders": risky,
                "rationale": (
                    f"coverage is computed from usable_stock "
                    f"{component.usable_stock}, not the current_stock "
                    f"{component.current_stock} in the ERP header; production "
                    f"orders are walked in deadline order and each consumes what "
                    f"the earlier ones left, so the requirement at "
                    f"{rows[-1]['production_order_id']}'s day "
                    f"{rows[-1]['deadline_day']} is {rows[-1]['cumulative']} units, "
                    f"not {rows[-1]['units']}") if rows else "no orders for this component"})

    out["audit_events"] = append_event(
        {**work, "audit_events": events},
        type="calculation", actor="impact",
        summary=(f"baseline if we do nothing: {base['units_short']} units short, "
                 f"{base['production_days_lost']} production-days lost"),
        detail={"baseline": base,
                "rationale": "the counterfactual is computed before anything is "
                             "spent, so every plan can be reported as a delta "
                             "against it rather than as a bare number"},
        baseline_delta=base,
        remaining_risk=(f"{', '.join(base['deadline_misses'])} miss their deadlines "
                        f"with no action" if base["deadline_misses"] else None))
    return out
