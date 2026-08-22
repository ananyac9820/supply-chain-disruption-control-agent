"""Node 2 — impact + baseline. NO LLM.

Writes: usable_stock, coverage_days, at_risk_orders, baseline

Real logic (hour 6):
    coverage_days = usable_stock / daily_usage      # 390 / 90 = 4.33

ALWAYS from usable_stock, never current_stock. That one line is the difference
between passing and failing PS Scenario 2 / H-02.

baseline is the counterfactual — what happens if the agent does nothing:
    {units_short, production_days_lost, deadline_misses, cost_of_inaction}
Every plan is later reported as a delta against it, which is what gives Cost
Control a denominator.

HOLLOW PASS: writes its four keys with placeholder numbers. The arithmetic
below is a stub, not the computation — node 2 owns no LLM call and its real
version derives every figure from the sandbox reads.
"""

from __future__ import annotations

from agent.audit import append_event
from contracts.state import AgentState


def impact(state: AgentState) -> dict:
    # STUB — hour 6 reads /inventory and /production-schedule and computes.
    out: dict = {
        "usable_stock": 0,
        "coverage_days": 0.0,
        "at_risk_orders": [],
        "baseline": {
            "units_short": 0,
            "production_days_lost": 0.0,
            "deadline_misses": [],
            "cost_of_inaction": 0.0,
        },
    }

    out["audit_events"] = append_event(
        state,
        type="calculation",
        actor="impact",
        summary="STUB impact and baseline (hollow node 2)",
        detail={"stub": True, "node": "impact",
                "note": "coverage must come from usable_stock, never current_stock"},
    )
    return out
