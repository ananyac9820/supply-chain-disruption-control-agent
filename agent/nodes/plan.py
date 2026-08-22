"""Node 4 — plan. NO LLM for the numbers.

Writes: plan, assumptions, rejected_alternatives, guardrail_verdicts,
        replan_count

    solver_input = build_solver_input(state)   # deterministic assembly
    out          = solve(solver_input)         # Person A's CP-SAT
    verdict      = validate(out, context)      # Person A's G1-G12

The LLM's only role here is writing the rationale AFTER the solver has
answered. It does not choose the split, the reschedule or the cost, and it
does not overrule the validator: on a veto the graph re-solves, at most twice,
then escalates.

THE VERIFICATION COUNTERFACTUAL
When node 3 has contradicted a supplier's claim, this node solves twice: once
for real, and once with the contradiction flags cleared. The difference is
what the verification was worth, in rupees, computed rather than asserted.
On the COMP-104 scenario it comes out at 8,690 — and because that delta is
what pushes the plan over the 150,000 threshold, it is also the reason the run
pauses for a human at all. The decision brief states it in exactly those
terms. The counterfactual costs no tool budget: solve() is an internal
function, not a metered endpoint.

REJECTED ALTERNATIVES
Built in agent/solver_input.py, which is the only place that knows both the
rule and the supplier it removed, and written here because node 4 is the only
node that has the complete list. PS §4.10 requires alternatives considered,
and "SUP-18, rejected: missing Automotive-Grade certification" is the shape.

COUNTER NOTE: AgentState is frozen with one counter, replan_count, which §4.5
defines as the assumption-break counter capped at 3. The §4.1 validator-veto
correction rounds are a different counter with a different cap (2), so they
live in plan["correction_rounds"] rather than as a new AgentState key. The
G8 re-RFQ path shares replan_count, because it is a trip back to node 3 like
any other replan.
"""

from __future__ import annotations

from agent import clock
from agent.audit import append_event
from agent.integrations import (GUARDRAILS_AVAILABLE, SOLVER_AVAILABLE, solve,
                                validate)
from agent.llm import get_llm
from agent.solver_input import (apply_commitments, build_solver_input,
                                commitments_from, validator_context)
from agent.tools import call_tool
from contracts.constants import APPROVAL_THRESHOLD
from contracts.state import AgentState

MAX_CORRECTION_ROUNDS = 2       # §4.1 — then escalate, never a third re-solve


def plan(state: AgentState) -> dict:
    work = dict(state)
    previous = work.get("plan") or {}
    rounds = int(previous.get("correction_rounds", 0))
    revising = bool(previous) and not work.get("_fresh_quotes")

    component_id = work.get("affected_component") or ""
    inventory = call_tool(work, "get_inventory",
                          "the solver reasons from usable_stock and the quality "
                          "floor, both of which live on the component record",
                          component_id=component_id)
    suppliers = call_tool(work, "get_suppliers",
                          "the candidate set and its MOQs, lead times and "
                          "reliabilities are the solver's decision variables",
                          component_id=component_id)
    schedule = call_tool(work, "get_production_schedule",
                         "the deadlines and priorities the plan has to satisfy")

    component = inventory[0]
    now = clock.now()
    quotes = list(work.get("quotes") or [])
    claims = list(work.get("claims") or [])

    # Is the plan we already have still standing? Within a single pass this
    # question is trivially yes — the solver filters expired quotes on the same
    # clock the input was built with, so no allocation it produces can rest on
    # a stale one. G8 becomes reachable only when a plan OUTLIVES its quotes:
    # it sits at the approval gate while a coordinator takes longer than
    # quote_valid_hours, and the run comes back here. That is the case this
    # re-check catches, and it is the realistic one — six hours is not long.
    stale = _recheck_previous(work, previous, quotes, claims, now)
    if stale is not None:
        return stale

    solver_input, rejected = build_solver_input(
        component=component, suppliers=suppliers, production_orders=schedule,
        quotes=quotes, claims=claims, now=now,
        reserved_budget=float(work.get("reserved_budget") or 0.0))

    # §4.5 replan hygiene: re-solve for the shortfall ONLY. Units already
    # committed by node 6 stay committed and come off both sides of the
    # problem - the demand they cover and the supply they consumed.
    commitments = commitments_from(work.get("erp_writes") or [])
    if commitments:
        netted, trimmed = apply_commitments(
            solver_input.production_orders, solver_input.suppliers, commitments)
        solver_input = solver_input.model_copy(
            update={"production_orders": netted, "suppliers": trimmed})
        work["audit_events"] = append_event(
            work, type="calculation", actor="planner",
            summary=(f"re-solving for the shortfall only: "
                     f"{sum(c['units'] for c in commitments)} units already "
                     f"committed stay committed"),
            detail={"commitments": commitments,
                    "remaining_requirement": [o.model_dump() for o in netted],
                    "rationale": "tearing up a good purchase order because a "
                                 "different supplier moved is how a replan costs "
                                 "more than the disruption did"})

    work["audit_events"] = append_event(
        work, type="calculation", actor="planner",
        summary=(f"solver input assembled: {len(solver_input.suppliers)} eligible "
                 f"suppliers, {len(solver_input.production_orders)} orders, "
                 f"budget cap {solver_input.budget_cap:,.0f}"),
        detail={"component_id": solver_input.component_id,
                "usable_stock": solver_input.usable_stock,
                "safety_stock": solver_input.safety_stock,
                "min_quality": solver_input.min_quality,
                "budget_cap": solver_input.budget_cap,
                "approval_limit": solver_input.approval_limit,
                "suppliers": [s.model_dump() for s in solver_input.suppliers],
                "production_orders": [o.model_dump()
                                      for o in solver_input.production_orders],
                "rationale": "assembled deterministically; claim_contradicted and "
                             "effective_reliability are how node 3's verification "
                             "reaches the decision"},
        alternatives_rejected=rejected)

    if not SOLVER_AVAILABLE:
        return _no_solver(work, rejected, rounds)

    out = solve(solver_input)
    # A counterfactual against an infeasible plan compares a cost to
    # zero and reports a negative saving, which is worse than silence.
    counterfactual = (_counterfactual(solver_input)
                      if out.status != "INFEASIBLE" else None)

    verdict = _validate(solver_input, out, quotes, claims, now, work)
    new_plan = _as_plan(out, rounds, revising, counterfactual, solver_input)

    llm = get_llm()
    rationale = llm.explain_plan({
        "status": out.status,
        "relaxation_used": out.relaxation_used,
        "allocations": [a.model_dump() for a in out.allocations],
        "reschedules": [r.model_dump() for r in out.reschedules],
        "total_cost": out.total_cost,
        "binding_constraint": out.binding_constraint,
        "baseline": work.get("baseline"),
        "rejected_alternatives": rejected,
        "verification_delta": counterfactual,
        "verdict": verdict,
    })
    new_plan["rationale"] = rationale.rationale
    new_plan["why_not_alternatives"] = rationale.why_not_alternatives

    out_state: dict = {
        "plan": new_plan,
        "rejected_alternatives": rejected,
        "guardrail_verdicts": list(work.get("guardrail_verdicts") or []) + [verdict],
        "assumptions": _register(work, out, solver_input, now),
        "replan_count": int(work.get("replan_count", 0)),
        "tools_called": work["tools_called"],
        "tool_budget_remaining": work["tool_budget_remaining"],
    }

    # G8: the plan rests on an expired quote. Drop the stale ones and send the
    # run back to node 3 for a fresh RFQ rather than re-solving on a dead price.
    if "G8" in (verdict.get("fired") or []):
        stale = [q["supplier_id"] for q in quotes
                 if any(r["supplier_id"] == q["supplier_id"] and r["rule"] == "G8"
                        for r in rejected)]
        out_state["quotes"] = [q for q in quotes if q["supplier_id"] not in stale]
        out_state["broken_assumption"] = (
            f"G8: quote expired for {', '.join(stale) or 'a supplier in the plan'}")
        out_state["replan_count"] = int(work.get("replan_count", 0)) + 1

    events = append_event(
        work, type="plan_proposed", actor="planner",
        summary=(f"{out.status} - relaxation={out.relaxation_used} - "
                 f"{out.total_cost:,.0f}"
                 + (f" - vs baseline {(work.get('baseline') or {}).get('cost_of_inaction', 0):,.0f}"
                    if work.get("baseline") else "")),
        detail={"status": out.status, "relaxation_used": out.relaxation_used,
                "allocations": [a.model_dump() for a in out.allocations],
                "reschedules": [r.model_dump() for r in out.reschedules],
                "total_cost": out.total_cost,
                "binding_constraint": out.binding_constraint,
                "requires_approval": out.requires_approval,
                "verification_delta": counterfactual,
                "correction_round": new_plan["correction_rounds"],
                "solver": "track-a" if SOLVER_AVAILABLE else "unavailable",
                "rationale": new_plan["rationale"]},
        alternatives_rejected=rejected,
        baseline_delta=_delta(work, out))

    if verdict.get("fired"):
        events = append_event(
            {**work, "audit_events": events}, type="guardrail", actor="validator",
            summary=f"{', '.join(verdict['fired'])} fired: {'; '.join(verdict['reasons'])}",
            detail={"fired": verdict["fired"], "passed": verdict["passed"],
                    "forced_escalation": verdict["forced_escalation"],
                    "reasons": verdict["reasons"],
                    "validator": "track-a" if GUARDRAILS_AVAILABLE else "unavailable",
                    "rationale": "the validator is deterministic and the model does "
                                 "not get to overrule it"})
    out_state["audit_events"] = events
    return out_state


# ---- solving -----------------------------------------------------------

def _recheck_previous(work: dict, previous: dict, quotes: list[dict],
                      claims: list[dict], now) -> dict | None:
    """Re-validate an existing plan against the current clock.

    Returns the routing state when G8 fires — the expired quotes are dropped,
    broken_assumption names them, and replan_count ticks so the re-RFQ path is
    capped like any other replan. Returns None when there is nothing to
    re-check or the plan still stands, and node 4 proceeds to solve normally.

    Only G8 short-circuits here. A stale price is the one finding a fresh RFQ
    fixes; anything else the full solve below will surface again anyway.
    """
    if not GUARDRAILS_AVAILABLE or not previous or not previous.get("allocations"):
        return None
    from contracts.models import SolverOutput

    plan_obj = SolverOutput(
        status=previous.get("status", "FEASIBLE"),
        allocations=previous.get("allocations") or [],
        reschedules=previous.get("reschedules") or [],
        total_cost=float(previous.get("total_cost") or 0.0),
        requires_approval=bool(previous.get("requires_approval")),
        binding_constraint=previous.get("binding_constraint"),
        relaxation_used=previous.get("relaxation_used"))
    verdict = validate(plan_obj, {
        "approval_limit": float(APPROVAL_THRESHOLD),
        "quotes": quotes, "claims": claims, "now": now,
    }).model_dump()

    if "G8" not in (verdict.get("fired") or []):
        return None

    expired = sorted({a["supplier_id"] for a in previous["allocations"]
                      if _is_stale(quotes, a["supplier_id"], now)})
    return {
        "plan": previous,
        "guardrail_verdicts": list(work.get("guardrail_verdicts") or []) + [verdict],
        "quotes": [q for q in quotes if q["supplier_id"] not in expired],
        "broken_assumption": f"G8: quote expired for {', '.join(expired)}",
        "replan_count": int(work.get("replan_count", 0)) + 1,
        "tools_called": work["tools_called"],
        "tool_budget_remaining": work["tool_budget_remaining"],
        "audit_events": append_event(
            work, type="guardrail", actor="validator",
            summary=(f"G8 fired: quote expired for {', '.join(expired)} - "
                     f"re-RFQ, do not re-solve"),
            detail={"fired": verdict["fired"], "reasons": verdict["reasons"],
                    "expired_suppliers": expired,
                    "rationale": "a stale quote is fixed by asking for a fresh "
                                 "one, not by solving again on a dead price; the "
                                 "rest of the plan stands"},
            remaining_risk=f"{', '.join(expired)} must re-quote before this plan "
                           f"can execute"),
    }


def _is_stale(quotes: list[dict], supplier_id: str, now) -> bool:
    from agent.solver_input import quote_expired
    quote = next((q for q in quotes if q["supplier_id"] == supplier_id), None)
    return bool(quote and quote_expired(quote, now))


def _counterfactual(solver_input) -> dict | None:
    """What the plan would have cost if node 3 had not checked the claim.

    A contradicted supplier stays in the solver input carrying its flag, so the
    counterfactual is the same world with that one flag cleared: one deep copy,
    one extra solve. It costs no tool budget, because solve() is a function and
    not a metered endpoint, and it turns "verification matters" into a number
    that can be put in front of a judge.
    """
    contradicted = sorted(s.supplier_id for s in solver_input.suppliers
                          if s.claim_contradicted)
    if not contradicted:
        return None
    naive = solver_input.model_copy(deep=True)
    for s in naive.suppliers:
        s.claim_contradicted = False
    unchecked = solve(naive)
    if unchecked.status == "INFEASIBLE":
        return None
    return {
        "suppliers_excluded": contradicted,
        "cost_if_unverified": unchecked.total_cost,
        "cost_as_planned": None,        # filled by _as_plan, which has both
        "note": "the difference is what verifying the claim was worth",
    }


def _validate(solver_input, out, quotes, claims, now, work) -> dict:
    if not GUARDRAILS_AVAILABLE:
        return {"passed": False, "fired": [], "forced_escalation": True,
                "reasons": ["guardrails/ not importable yet; failing safe to "
                            "escalation rather than executing unvalidated"]}
    context = validator_context(
        solver_input, out, quotes, claims, now,
        affected_priorities=_affected_priorities(work, out))
    verdict = validate(out, context)
    return verdict.model_dump()


def _affected_priorities(work: dict, out) -> list[str]:
    """Priorities of the orders this plan touches, for G5.

    Defaults to "high" for an unknown order rather than "low": G5 escalates a
    safety-stock breach on a high-priority order, and guessing low would
    silently suppress the escalation the rule exists to force.
    """
    base = work.get("baseline") or {}
    lookup = base.get("priorities") or {}
    touched = set(base.get("deadline_misses") or []) | {
        r.production_order_id for r in out.reschedules}
    return sorted({lookup.get(pid, "high") for pid in touched}) or ["high"]


def _as_plan(out, rounds: int, revising: bool, counterfactual: dict | None,
             solver_input) -> dict:
    plan_dict = {
        "plan_id": f"PLAN-{rounds + 1:03d}",
        "status": out.status,
        "allocations": [a.model_dump() for a in out.allocations],
        "reschedules": [r.model_dump() for r in out.reschedules],
        "total_cost": out.total_cost,
        "estimated_cost": out.total_cost,          # node 5 reads this
        "requires_approval": out.requires_approval,
        "binding_constraint": out.binding_constraint,
        "relaxation_used": out.relaxation_used,
        "correction_rounds": rounds + 1 if revising else 0,
        "budget_cap": solver_input.budget_cap,
        "min_quality": solver_input.min_quality,
        "solver_called": SOLVER_AVAILABLE,
        "validator_called": GUARDRAILS_AVAILABLE,
    }
    if counterfactual:
        counterfactual["cost_as_planned"] = out.total_cost
        counterfactual["delta"] = round(
            out.total_cost - counterfactual["cost_if_unverified"], 2)
        counterfactual["crossed_threshold"] = bool(
            counterfactual["cost_if_unverified"] <= APPROVAL_THRESHOLD
            < out.total_cost)
        plan_dict["verification_delta"] = counterfactual
    return plan_dict


def _delta(work: dict, out) -> dict | None:
    base = work.get("baseline") or {}
    if not base:
        return None
    inaction = float(base.get("cost_of_inaction") or 0.0)
    return {"cost_of_inaction": inaction, "plan_cost": out.total_cost,
            "net_avoided": round(inaction - out.total_cost, 2),
            "production_days_recovered": base.get("production_days_lost")}


def _register(work: dict, out, solver_input, now) -> list[dict]:
    """The facts this plan depends on (§4.4). The watcher that re-checks them
    and routes a break back into node 3 lands at hour 14; the register itself
    is here because node 4 is where the dependencies become knowable."""
    assumptions = []
    for i, a in enumerate(out.allocations, start=1):
        quote = next((q for q in work.get("quotes") or []
                      if q["supplier_id"] == a.supplier_id), None)
        assumptions.append({
            "id": f"A{i}",
            "claim": (f"{a.supplier_id} supplies {a.units} units at "
                      f"{(quote or {}).get('unit_price', '?')} arriving day "
                      f"{a.arrival_day}"),
            "source": "rfq" if quote else "catalog",
            "verified": quote is not None,
            "expires_at": _expiry(quote),
        })
    assumptions.append({
        "id": f"A{len(assumptions) + 1}",
        "claim": (f"usable_stock {solver_input.component_id} = "
                  f"{solver_input.usable_stock}"),
        "source": "inventory", "verified": True, "expires_at": None})
    return assumptions


def _expiry(quote: dict | None) -> str | None:
    if not quote:
        return None
    from datetime import datetime, timedelta
    issued = quote.get("issued_at")
    if isinstance(issued, str):
        issued = datetime.fromisoformat(issued)
    if issued is None:
        return None
    return (issued + timedelta(hours=int(quote.get("quote_valid_hours") or 0))).isoformat()


def _no_solver(work: dict, rejected: list[dict], rounds: int) -> dict:
    """Pre-merge path: solver/ is not importable. Fail safe to escalation
    rather than inventing a plan."""
    verdict = {"passed": False, "fired": [], "forced_escalation": True,
               "reasons": ["solver/ not importable yet; no plan can be produced"]}
    stub = {"plan_id": f"PLAN-{rounds + 1:03d}", "status": "SOLVER_UNAVAILABLE",
            "allocations": [], "reschedules": [], "total_cost": 0.0,
            "estimated_cost": 0.0, "requires_approval": True,
            "correction_rounds": rounds, "solver_called": False,
            "validator_called": GUARDRAILS_AVAILABLE,
            "rationale": "solver/ lands at the hour-12 merge; this run produced "
                         "no plan and escalates rather than executing"}
    return {
        "plan": stub,
        "rejected_alternatives": rejected,
        "guardrail_verdicts": list(work.get("guardrail_verdicts") or []) + [verdict],
        "assumptions": list(work.get("assumptions") or []),
        "replan_count": int(work.get("replan_count", 0)),
        "tools_called": work["tools_called"],
        "tool_budget_remaining": work["tool_budget_remaining"],
        "audit_events": append_event(
            work, type="plan_proposed", actor="planner",
            summary="no plan: solver/ is not importable before the hour-12 merge",
            detail={"solver_available": False, "rationale": stub["rationale"]},
            alternatives_rejected=rejected),
    }
