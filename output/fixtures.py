"""Synthetic audit-trail generator.

Building the renderers against a fixture rather than a live run is the whole
reason the output layer is never blocked on the graph. This emits a realistic
20-event trail for the run the demo tells:

    a supplier delay is detected  ->  the reply is vague and gets challenged
    ->  the dispatch claim is contradicted by tracking  ->  trust drops and
    SUP-21 leaves the candidate set  ->  procurement-only is INFEASIBLE
    ->  the reschedule rung of the ladder is feasible  ->  cost crosses
    150,000 and the run pauses for a coordinator.

Every figure below is derived from contracts/stub_sandbox.py as it stands
after the pre-freeze fixes, so the fixture and a real run tell the same story.

    usable_stock 390 - safety_stock 150      ->  240 units free of safety stock
    PROD-914  700 units, due day 2 (2026-09-04), low,  max_delay 5
    PROD-882  700 units, due day 4 (2026-09-06), high, max_delay 0

Continuity is CUMULATIVE: production orders are walked in deadline order and
each consumes what the earlier ones left, so the requirement at PROD-882's
day 4 is 1400 units, not 700. Reasoning per-order instead would understate
the gap and put node 2's numbers at odds with the solver's.

    rung 1, no reschedule: PROD-914 needs 700 by day 2; nothing certified
        arrives before day 4, so 240 < 700  ->  INFEASIBLE, binding on the
        PROD-914 deadline.
    rung 2, reschedule: PROD-882 is fixed at day 4 (max_delay 0) and needs
        460 bought from a day-4 arrival. Delaying PROD-914 by 4 days moves
        the cumulative 1400 to day 6, where 240 + 1450 available clears it.
        Delay 3 leaves only 240 + 1050, so 4 is the minimum.

    SUP-18 fails the 0.90 quality floor (0.79); SUP-21 is CONTRADICTED, so
    the certified, quality-passing, trusted set is SUP-42 / SUP-55 / SUP-37.
    Cheapest 1160 units that still puts >= 460 on a day-4 arrival is
    700 x SUP-42 + 350 x SUP-55 + 110 x SUP-37 = 152,010 -- over the 150,000
    threshold by 2,010, which is what fires G2. The 460/350/350 split costs
    154,170 and loses on the full objective too (199,146 vs 197,658).

    python -m output.fixtures [path]
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from contracts.constants import APPROVAL_THRESHOLD, TOOL_BUDGET_PER_DISRUPTION
from output.audit import AuditLog

T0 = datetime(2026, 9, 2, 10, 14, 2)

BASELINE = {
    "units_short": 1160,
    "production_days_lost": 12.89,
    "deadline_misses": ["PROD-914", "PROD-882"],
    "cost_of_inaction": 496_000.0,
}

PLAN_COST = 152_010.0
REJECTED = [
    {"supplier_id": "SUP-18",
     "reason": "quality 0.79 below the 0.90 floor for COMP-104 (G4)"},
    {"supplier_id": "SUP-21",
     "reason": "dispatch claim CONTRADICTED by tracking; units count as 0 confirmed (G9)"},
]


def _t(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def generate(path: str | Path | None = None, disruption_id: str = "DIS-001") -> Path:
    """Write the 20-event fixture and return its path."""
    log = AuditLog(disruption_id,
                   path=path or Path("audit_logs") / "fixture-DIS-001.jsonl")
    budget = TOOL_BUDGET_PER_DISRUPTION
    used = 0

    def tool(secs, endpoint, why, *, cached=False, actor="investigator", detail=None):
        nonlocal used
        if not cached:
            used += 1
        log.emit(
            ts=_t(secs), type="tool_call", actor=actor,
            summary=endpoint + ("  (cache hit, budget not charged)" if cached else ""),
            detail={"endpoint": endpoint, "served_from_cache": cached,
                    "budget_used": used, "budget_total": budget, **(detail or {})},
            tools_used=[endpoint], necessity=why,
        )

    # 1 - the opening event. PS 4.10 "detected disruption".
    log.emit(ts=_t(0), type="disruption_detected", actor="monitor",
             summary="PO-7712 delayed 5-7d - COMP-104",
             detail={"po_id": "PO-7712", "component_id": "COMP-104",
                     "disruption_type": "supplier_delay", "severity": "high",
                     "source": "inbox message MSG-0001 from SUP-21",
                     "rationale": "severity is high because PROD-882 is a high-priority "
                                  "order inside the coverage window, not because the "
                                  "delay is long"})

    # 2-4 - the deterministic scan.
    tool(0.4, "GET /purchase-orders/PO-7712",
         "the inbox claims a delay; the PO record is the system of truth for it")
    tool(0.8, "GET /inventory/COMP-104",
         "coverage cannot be computed without usable_stock")
    tool(1.2, "GET /production-schedule",
         "severity depends on which production orders sit inside the coverage window")

    # 5-6 - node 2. No LLM. PS 4.10 "calculations performed".
    log.emit(ts=_t(1.6), type="calculation", actor="impact",
             summary="coverage 4.33 days - PROD-914 (low) at risk in 2d, PROD-882 (high) in 4d",
             detail={"usable_stock": 390, "current_stock": 420, "daily_usage": 90,
                     "coverage_days": 4.33,
                     "free_of_safety_stock": 240,
                     "cumulative_requirement": [
                         {"production_order_id": "PROD-914", "deadline_day": 2,
                          "units": 700, "cumulative": 700, "cumulative_shortfall": 460},
                         {"production_order_id": "PROD-882", "deadline_day": 4,
                          "units": 700, "cumulative": 1400, "cumulative_shortfall": 1160},
                     ],
                     "at_risk_orders": ["PROD-914", "PROD-882"],
                     "rationale": "coverage is computed from usable_stock 390, not the "
                                  "current_stock 420 in the ERP header; orders are walked "
                                  "in deadline order and each consumes what the earlier "
                                  "ones left, so the day-4 requirement is 1400 not 700"})
    log.emit(ts=_t(1.9), type="calculation", actor="impact",
             summary="baseline if we do nothing: 1160 units short, 12.9 production-days lost",
             detail={"baseline": BASELINE,
                     "rationale": "the counterfactual is computed before anything is "
                                  "spent, so every plan can be reported as a delta"},
             baseline_delta=BASELINE)

    # 7-9 - challenge the vague reply (PS 4.4).
    tool(3.0, "POST /suppliers/SUP-21/message",
         "the delay window 5-7d is not a date; the plan cannot rest on it")
    log.emit(ts=_t(3.4), type="verification", actor="verification_agent",
             summary='SUP-21 reply "we are looking into this" -> VAGUE',
             detail={"classification": "VAGUE",
                     "reply": "We are looking into this and will update you soon.",
                     "verdict": "VAGUE",
                     "rationale": "no date and no quantity commitment, so the plan does "
                                  "not advance on it; alternate sourcing proceeds in "
                                  "parallel rather than waiting"})
    tool(4.1, "POST /suppliers/SUP-21/message",
         "one targeted follow-up naming PO-7712 and demanding a date and a quantity")

    # 10 - the cache hit. Tool Efficiency evidence.
    tool(4.4, "GET /inventory/COMP-104",
         "re-read inside the 30s cache window while composing the RFQ", cached=True)

    # 11-13 - the false claim and the contradiction (PS S-3 / H-08).
    log.emit(ts=_t(4.8), type="verification", actor="verification_agent",
             summary='SUP-21 now claims "dispatched" - unverified until tracking agrees',
             detail={"claim": "dispatched", "verdict": "UNVERIFIABLE",
                     "rationale": "a supplier claim is not evidence; tracking is"})
    tool(5.2, "GET /tracking/PO-7712",
         "supplier claim must be grounded before it can support the plan")
    log.emit(ts=_t(5.4), type="verification", actor="verification_agent",
             summary='SUP-21 "dispatched" vs label_created_no_pickup -> CONTRADICTED',
             detail={"supplier_id": "SUP-21", "claim": "dispatched",
                     "evidence": {"tracking_status": "label_created_no_pickup",
                                  "last_movement": None},
                     "verdict": "CONTRADICTED",
                     "trust_before": 0.72, "trust_after": 0.58,
                     "rationale": "trust_write(SUP-21, contradicted_claim); the new score "
                                  "is used in this same solve, not merely the next run"},
             tools_used=["GET /tracking/PO-7712"],
             remaining_risk="SUP-21's 600 units now count as 0 confirmed")

    # 14-16 - alternate sourcing.
    tool(6.0, "GET /suppliers?component_id=COMP-104",
         "SUP-21 is out of the candidate set; the gap must be sourced elsewhere")
    tool(6.6, "POST /rfq",
         "catalog price is not a commitment; a quote with a validity window is")
    log.emit(ts=_t(6.9), type="calculation", actor="planner",
             summary="gap 1160 units - 240 free of safety stock - 3 certified quotes",
             detail={"cumulative_requirement_units": 1400, "usable_stock": 390,
                     "safety_stock": 150, "free_of_safety_stock": 240, "gap": 1160,
                     "candidates": ["SUP-42", "SUP-55", "SUP-37"],
                     "rationale": "SUP-18 fails the quality floor and SUP-21 is "
                                  "contradicted, so neither enters the solver input"})

    # 17-18 - the ladder. PS 4.10 "decision" + "alternatives considered".
    log.emit(ts=_t(7.2), type="decision", actor="solver",
             summary="INFEASIBLE - relaxation=none - binding: PROD-914 day-2 deadline",
             detail={"status": "INFEASIBLE", "relaxation_used": "none",
                     "binding_constraint": "no certified supplier delivers before day 4; "
                                           "PROD-914 needs 700 units by day 2 and only "
                                           "240 are free of safety stock",
                     "rationale": "rung 1 of the ladder is procurement only, with every "
                                  "r[p] pinned at 0"})
    log.emit(ts=_t(7.5), type="decision", actor="solver",
             summary=f"FEASIBLE - relaxation=reschedule - {PLAN_COST:,.0f} - vs baseline 496,000",
             detail={"status": "FEASIBLE", "relaxation_used": "reschedule",
                     "allocations": [
                         {"supplier_id": "SUP-42", "units": 700, "cost": 92_400.0,
                          "arrival_day": 4},
                         {"supplier_id": "SUP-55", "units": 350, "cost": 44_100.0,
                          "arrival_day": 5},
                         {"supplier_id": "SUP-37", "units": 110, "cost": 15_510.0,
                          "arrival_day": 6},
                     ],
                     "reschedules": [{"production_order_id": "PROD-914", "delay_days": 4}],
                     "total_cost": PLAN_COST,
                     "rationale": "PROD-882 is high priority and cannot move, so 460 of "
                                  "its 700 units must come off the day-4 arrival. Delaying "
                                  "the low-priority PROD-914 by 4 days moves the cumulative "
                                  "1400 to day 6, where the day-5 and day-6 arrivals clear "
                                  "it. Three days is not enough; PROD-882 is untouched"},
             alternatives_rejected=REJECTED,
             baseline_delta={"cost_of_inaction": BASELINE["cost_of_inaction"],
                             "plan_cost": PLAN_COST,
                             "net_avoided": BASELINE["cost_of_inaction"] - PLAN_COST,
                             "production_days_recovered": 12.89})

    # 19-20 - the guardrail and the pause. PS 4.10 "escalations" + "remaining risks".
    over = PLAN_COST - APPROVAL_THRESHOLD
    log.emit(ts=_t(7.7), type="guardrail", actor="validator",
             summary=f"G2 fired: cost exceeds {APPROVAL_THRESHOLD:,} by {over:,.0f} -> escalation required",
             detail={"fired": ["G2"], "passed": False, "forced_escalation": True,
                     "plan_cost": PLAN_COST, "threshold": float(APPROVAL_THRESHOLD),
                     "rationale": "G2 is an absolute block, not a preference; the model "
                                  "does not get to overrule it"})
    log.emit(ts=_t(7.9), type="escalation", actor="gate",
             summary="interrupt() - awaiting coordinator - state checkpointed",
             detail={"plan_id": "PLAN-002", "estimated_cost": PLAN_COST,
                     "exceeds_threshold_by": over,
                     "options": ["approve", "edit", "reject"],
                     "rationale": "two-axis gate: high impact, and G2 forces escalation "
                                  "regardless of confidence"},
             remaining_risk="SUP-55 reliability is 0.55 and its MOQ equals its availability, "
                            "so a shortfall there cannot be topped up; re-check the SUP-42 "
                            "quote before it expires at T+6h")

    log.close()
    return log.path


def main() -> None:
    dest = sys.argv[1] if len(sys.argv) > 1 else None
    path = generate(dest)
    from output.audit import read_jsonl
    print(f"wrote {len(read_jsonl(path))} events -> {path}")


if __name__ == "__main__":
    main()
