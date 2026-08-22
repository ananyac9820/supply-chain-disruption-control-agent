"""Node 4 — plan. NO LLM for the numbers.

Writes: plan, assumptions, rejected_alternatives, guardrail_verdicts,
        replan_count

Real logic (hour 10.5):

    solver_input = build_solver_input(state)   # deterministic assembly
    out          = solve(solver_input)         # Person A's CP-SAT
    verdict      = validate(out, context)      # Person A's G1-G12

The LLM's only role is writing the rationale after the solver has answered,
and choosing whether to re-solve when the validator vetoes. Maximum two
correction rounds, then escalate. The LLM does not overrule the validator.

rejected_alternatives gets every filtered-out supplier and the reason
("SUP-18, rejected: missing Automotive-Grade certification"). PS §4.10 wants
alternatives considered in the audit trail and this is the only place that
list exists.

COUNTER NOTE: AgentState is frozen and carries one counter, replan_count,
which §4.5 defines as the assumption-break replan counter capped at 3. The
validator-veto correction rounds of §4.1 are a different counter with a
different cap (2), so they live inside the plan dict as
plan["correction_rounds"] rather than as a new AgentState key. Adding a key
here would be contract drift and would break the hour-12 merge.

HOLLOW PASS: writes its five keys. solve() and validate() are called only
when Track A's folders are importable; until then the guards in
agent/integrations.py keep this node from inventing a solver.
"""

from __future__ import annotations

from agent.audit import append_event
from agent.integrations import GUARDRAILS_AVAILABLE, SOLVER_AVAILABLE
from contracts.constants import APPROVAL_THRESHOLD
from contracts.state import AgentState

MAX_CORRECTION_ROUNDS = 2       # §4.1 — then escalate, never a third re-solve


def plan(state: AgentState) -> dict:
    previous = state.get("plan") or {}
    rounds = int(previous.get("correction_rounds", 0))
    revising = bool(previous)

    # STUB — hour 10.5:
    #   solver_input = build_solver_input(state)
    #   out = solve(solver_input)  if SOLVER_AVAILABLE
    #   verdict = validate(out, context)  if GUARDRAILS_AVAILABLE
    new_plan: dict = {
        # one id per solve attempt, so a veto loop or an edit produces
        # PLAN-001, PLAN-002, ... rather than colliding ids in the audit trail
        "plan_id": f"PLAN-{len(state.get('guardrail_verdicts') or []) + 1:03d}",
        "status": "STUB",
        "allocations": [],
        "reschedules": [],
        # Crosses APPROVAL_THRESHOLD on purpose so the hollow run exercises
        # the interrupt() edge at node 5. Drop it below 150,000 and the same
        # graph takes the auto-execute edge instead.
        "estimated_cost": float(APPROVAL_THRESHOLD) + 1.0,
        "correction_rounds": rounds + 1 if revising else 0,
        "solver_called": SOLVER_AVAILABLE,
        "validator_called": GUARDRAILS_AVAILABLE,
    }

    # A hollow verdict that passes. When guardrails/ lands, this becomes the
    # real Verdict and route_after_plan starts exercising the veto loop.
    verdict = {
        "passed": True,
        "fired": [],
        "reasons": ["hollow pass: guardrails/ not importable yet"
                    if not GUARDRAILS_AVAILABLE else "stub verdict"],
        "forced_escalation": False,
    }

    out: dict = {
        "plan": new_plan,
        "assumptions": list(state.get("assumptions") or []),
        "rejected_alternatives": list(state.get("rejected_alternatives") or []),
        "guardrail_verdicts": list(state.get("guardrail_verdicts") or []) + [verdict],
        "replan_count": int(state.get("replan_count", 0)),
    }

    out["audit_events"] = append_event(
        state,
        type="plan_proposed",
        actor="planner",
        summary=(f"STUB plan {new_plan['plan_id']} proposed "
                 f"(hollow node 4, correction round {new_plan['correction_rounds']})"),
        detail={"stub": True, "node": "plan",
                "solver_available": SOLVER_AVAILABLE,
                "guardrails_available": GUARDRAILS_AVAILABLE,
                "verdict": verdict},
    )
    return out
