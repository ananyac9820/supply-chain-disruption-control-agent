"""Node 5 — gate. The human checkpoint.

Writes: requires_approval, approval_reason, human_response

TWO AXES, NOT A COST THRESHOLD

    impact bracket  x  confidence     ->  auto-execute only in one cell

               confidence: high   medium    low
    impact low             AUTO   pause    pause
    impact medium          pause  pause    pause
    impact high            pause  pause    pause

A flat cost threshold can only answer "it cost more". Two axes answer the
question a judge actually asks — why did this one need a human and that one
didn't — with something checkable: this plan was high impact, or we weren't
confident enough in the evidence under it, and here is which.

    IMPACT is about what the plan does: does it cross the approval limit, move
    a high-priority production order, breach safety stock, or leave demand
    uncovered.

    CONFIDENCE is about what the plan rests on: was the investigation complete,
    is every supplier in it backed by a live quote, is any claim under it still
    unverified, did the solver need the partial-coverage rung.

Both are computed here, deterministically, from state. The model does not vote.

ALWAYS ESCALATE, whatever the brackets say:
    G2   cost over the approval threshold
    G5   safety-stock breach on a high-priority order
    G10  tool budget exhausted - the investigation is incomplete by definition
    G12  no feasible plan after the full relaxation ladder

RESUME SAFETY: on resume LangGraph re-executes this node from its start, so
everything above the interrupt() call must be safe to repeat. It is: the
assessment is a pure function of state and writes nothing. Side effects — the
ERP writes — are node 6's, after the human has answered.
"""

from __future__ import annotations

from langgraph.types import interrupt

from agent.audit import append_event
from contracts.constants import APPROVAL_THRESHOLD
from contracts.state import AgentState

ALWAYS_ESCALATE = {"G2", "G10", "G12"}      # G5 is conditional, see _impact


def gate(state: AgentState) -> dict:
    plan = state.get("plan") or {}
    verdict = _latest_verdict(state)

    # --- idempotent region: pure reads, safe to repeat on resume -----------
    impact, impact_why = _impact(state, plan, verdict)
    confidence, conf_why = _confidence(state, plan, verdict)
    forced = _forced(state, plan, verdict)

    auto = impact == "low" and confidence == "high" and not forced
    reason = "; ".join(forced or []) or None
    if not auto and reason is None:
        reason = (f"impact {impact} and confidence {confidence}; "
                  f"auto-execution needs low impact with high confidence")

    assessment = {
        "impact": impact, "impact_because": impact_why,
        "confidence": confidence, "confidence_because": conf_why,
        "forced_escalation": forced,
        "auto_executed": auto,
    }

    if auto:
        out: dict = {
            "requires_approval": False,
            "approval_reason": None,
            "human_response": {"decision": "auto",
                               "note": "low impact, high confidence"},
        }
        out["audit_events"] = append_event(
            state, type="decision", actor="gate",
            summary=("auto-execute: low impact, high confidence - "
                     + "; ".join(impact_why + conf_why)),
            detail={**assessment,
                    "rationale": "both axes cleared and no rule forces a human, "
                                 "so waking one would be theatre"})
        return out

    # --- the pause ---------------------------------------------------------
    # Nothing below runs until a coordinator resumes with
    # Command(resume={"decision": ...}).
    from output.brief import render_brief

    brief = render_brief(state, assessment=assessment)
    decision = interrupt({
        "kind": "approval_required",
        "disruption_id": state.get("disruption_id"),
        "plan_id": plan.get("plan_id"),
        "estimated_cost": plan.get("estimated_cost"),
        "impact": impact,
        "confidence": confidence,
        "reason": reason,
        "brief": brief,
        "options": ["approve", "edit", "reject"],
    })

    human = decision if isinstance(decision, dict) else {"decision": str(decision)}

    return {
        "requires_approval": True,
        "approval_reason": reason,
        "human_response": human,
        "audit_events": append_event(
            state, type="escalation", actor="gate",
            summary=(f"escalated ({impact} impact / {confidence} confidence) - "
                     f"coordinator chose {human.get('decision')}"),
            detail={**assessment, "reason": reason,
                    "human_response": human, "brief": brief,
                    "rationale": "a two-axis gate can say why this one needed a "
                                 "human and another did not"},
            remaining_risk=_remaining_risk(state, plan)),
    }


# ---- the two axes ------------------------------------------------------

def _impact(state: AgentState, plan: dict, verdict: dict) -> tuple[str, list[str]]:
    """What the plan does."""
    why: list[str] = []
    cost = float(plan.get("estimated_cost") or 0.0)
    base = state.get("baseline") or {}
    priorities = base.get("priorities") or {}
    reschedules = plan.get("reschedules") or []

    high = False
    if cost > APPROVAL_THRESHOLD:
        high = True
        why.append(f"cost {cost:,.0f} exceeds the {APPROVAL_THRESHOLD:,} threshold")
    if plan.get("status") == "INFEASIBLE":
        high = True
        why.append(f"no feasible plan; binding on {plan.get('binding_constraint')}")
    if plan.get("relaxation_used") == "partial":
        high = True
        why.append("only partial coverage was achievable")
    for r in reschedules:
        if priorities.get(r.get("production_order_id")) == "high":
            high = True
            why.append(f"{r['production_order_id']} is high priority and moves "
                       f"{r.get('delay_days')} days")
    if high:
        return "high", why

    medium = False
    if reschedules:
        medium = True
        why.append(f"{len(reschedules)} production order(s) reschedule")
    if cost > APPROVAL_THRESHOLD / 2:
        medium = True
        why.append(f"cost {cost:,.0f} is over half the approval threshold")
    if medium:
        return "medium", why

    why.append(f"cost {cost:,.0f} well inside the threshold, nothing rescheduled")
    return "low", why


def _confidence(state: AgentState, plan: dict, verdict: dict) -> tuple[str, list[str]]:
    """What the plan rests on."""
    why: list[str] = []
    claims = state.get("claims") or []
    quotes = {q["supplier_id"] for q in state.get("quotes") or []}
    allocated = {a["supplier_id"] for a in plan.get("allocations") or []}

    if int(state.get("tool_budget_remaining", 1)) <= 0:
        why.append("tool budget exhausted; the investigation is incomplete")
        return "low", why
    if not plan.get("solver_called") or not plan.get("validator_called"):
        why.append("the plan was not produced and checked by the real solver "
                   "and validator")
        return "low", why

    unverified = [c["supplier_id"] for c in claims
                  if c.get("status") in ("PENDING", "UNVERIFIABLE")
                  and c["supplier_id"] in allocated]
    if unverified:
        why.append(f"{', '.join(unverified)} in the plan still rests on an "
                   f"unverified claim")
        return "low", why

    unquoted = sorted(allocated - quotes)
    if unquoted:
        why.append(f"{', '.join(unquoted)} priced from the catalog rather than "
                   f"a live quote")
        return "medium", why

    why.append("investigation complete, every supplier in the plan has a live "
               "quote, no claim under it is unverified")
    return "high", why


def _forced(state: AgentState, plan: dict, verdict: dict) -> list[str]:
    """Rules that escalate whatever the brackets say."""
    forced: list[str] = []
    fired = set(verdict.get("fired") or [])
    reasons = dict(zip(verdict.get("fired") or [], verdict.get("reasons") or []))

    for rule in sorted(fired & ALWAYS_ESCALATE):
        forced.append(f"{rule}: {reasons.get(rule, 'always escalates')}")

    if "G5" in fired:
        priorities = set((state.get("baseline") or {}).get("priorities", {}).values())
        if "high" in priorities:
            forced.append(f"G5 on a high-priority order: "
                          f"{reasons.get('G5', 'safety stock breached')}")

    if int(state.get("tool_budget_remaining", 1)) <= 0:
        forced.append("G10: tool budget exhausted, INCOMPLETE_INVESTIGATION")

    if verdict.get("forced_escalation") and not forced:
        forced.append("; ".join(verdict.get("reasons") or ["validator forced escalation"]))
    return forced


def _latest_verdict(state: AgentState) -> dict:
    verdicts = state.get("guardrail_verdicts") or []
    return verdicts[-1] if verdicts else {}


def _remaining_risk(state: AgentState, plan: dict) -> str | None:
    bits = []
    for a in plan.get("allocations") or []:
        quote = next((q for q in state.get("quotes") or []
                      if q["supplier_id"] == a["supplier_id"]), None)
        if quote:
            bits.append(f"{a['supplier_id']} quote expires "
                        f"{quote['quote_valid_hours']}h from issue")
    return "; ".join(bits) or None
