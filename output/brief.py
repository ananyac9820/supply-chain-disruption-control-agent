"""The decision brief — PS §17 shape, verbatim.

This is the leave-behind and the escalation payload. PS §4.9 asks for "a
concise decision brief, not a vague alert", and §17 wrote out the shape they
want, so the section headings below are theirs and are not to be improved:

    Disruption Detected / Actions Taken / Recommended Plan / Reasoning /
    Cost of No Action / Escalation Required / Remaining Risk

"Cost of No Action" is our addition and it is the section a real approver reads
first, because it is the only one that says what happens if they do nothing.

THE VERIFICATION LINE
When the run contradicted a supplier's claim, Reasoning states the delta as a
cause, not a curiosity: checking the claim is what changed the cost, and when
that change crossed the approval threshold it is also the reason the brief is
in front of a human at all. The number is computed by node 4's counterfactual
solve, not asserted here.

Renders from AgentState, so it works for an escalated run, an auto-executed
run and a replanned run alike.
"""

from __future__ import annotations

from contracts.constants import APPROVAL_THRESHOLD


def render_brief(state: dict, assessment: dict | None = None) -> str:
    plan = state.get("plan") or {}
    base = state.get("baseline") or {}
    sections = [
        _detected(state, base),
        _actions(state),
        _recommended(plan),
        _reasoning(state, plan),
        _no_action(base),
        _escalation(state, plan, assessment),
        _remaining(state, plan, assessment),
    ]
    return "\n\n".join(s for s in sections if s)


# ---- sections ----------------------------------------------------------

def _detected(state: dict, base: dict) -> str:
    rows = [
        f"{state.get('disruption_type', 'disruption')} on "
        f"{state.get('affected_component', '?')}, severity "
        f"{state.get('severity', '?')}",
        f"coverage {state.get('coverage_days', '?')} days from usable stock "
        f"{state.get('usable_stock', '?')}",
    ]
    at_risk = state.get("at_risk_orders") or []
    priorities = base.get("priorities") or {}
    if at_risk:
        rows.append("at risk: " + ", ".join(
            f"{pid} ({priorities.get(pid, '?')} priority)" for pid in at_risk))
    return "Disruption Detected:\n" + "\n".join(f"  {r}" for r in rows)


def _actions(state: dict) -> str:
    """Numbered, each naming the tool and what it found."""
    lines: list[str] = []
    for call in state.get("tools_called") or []:
        if call.get("served_from_cache"):
            continue                       # a cache hit found nothing new
        lines.append((call.get("endpoint") or call.get("tool"),
                      call.get("necessity", "")))

    findings = {}
    for reply in state.get("replies_received") or []:
        findings.setdefault("message", []).append(
            f"{reply['sender'].split('@')[0]} replied "
            f"{reply.get('classification', '?')}")
    for claim in state.get("claims") or []:
        findings.setdefault("tracking", []).append(
            f"{claim['supplier_id']} claim \"{claim['claim']}\" -> "
            f"{claim.get('status')}")
    if state.get("quotes"):
        findings.setdefault("rfq", []).append(
            f"{len(state['quotes'])} quotes returned")

    numbered = []
    for i, (endpoint, why) in enumerate(lines, start=1):
        note = ""
        if "message" in (endpoint or "") and findings.get("message"):
            note = " - " + findings["message"].pop(0)
        elif "tracking" in (endpoint or "") and findings.get("tracking"):
            note = " - " + findings["tracking"].pop(0)
        elif "rfq" in (endpoint or "") and findings.get("rfq"):
            note = " - " + findings["rfq"].pop(0)
        numbered.append(f"  {i}. {endpoint}{note}")
    return "Actions Taken:\n" + "\n".join(numbered) if numbered else ""


def _recommended(plan: dict) -> str:
    if plan.get("status") == "INFEASIBLE":
        return ("Recommended Plan:\n  none - no plan satisfies every constraint. "
                f"Binding constraint: {plan.get('binding_constraint')}")
    rows = []
    for a in plan.get("allocations") or []:
        rows.append(f"  {a['units']} units from {a['supplier_id']} at "
                    f"{a['cost'] / a['units']:,.2f}/unit, arriving day "
                    f"{a['arrival_day']} ({a['cost']:,.0f})")
    for r in plan.get("reschedules") or []:
        rows.append(f"  reschedule {r['production_order_id']} by "
                    f"{r['delay_days']} days")
    if plan.get("total_cost") is not None:
        rows.append(f"  total {plan['total_cost']:,.0f}")
    return "Recommended Plan:\n" + "\n".join(rows) if rows else ""


def _reasoning(state: dict, plan: dict) -> str:
    parts = []
    if plan.get("rationale"):
        parts.append(f"  {plan['rationale']}")
    rejected = state.get("rejected_alternatives") or []
    if rejected:
        parts.append("  Alternatives considered and rejected:")
        parts.extend(f"    {r.get('label') or r.get('supplier_id')}" for r in rejected)
    return "Reasoning:\n" + "\n".join(parts) if parts else ""


def _no_action(base: dict) -> str:
    if not base:
        return ""
    rows = [
        f"  {base.get('units_short', 0)} units short",
        f"  {base.get('production_days_lost', 0)} production-days lost",
    ]
    misses = base.get("deadline_misses") or []
    if misses:
        rows.append(f"  deadline misses: {', '.join(misses)}")
    if base.get("cost_of_inaction") is not None:
        rows.append(f"  cost of inaction {base['cost_of_inaction']:,.0f} "
                    f"over a {base.get('horizon_days', '?')}-day horizon")
    return "Cost of No Action:\n" + "\n".join(rows)


def _escalation(state: dict, plan: dict, assessment: dict | None) -> str:
    if not state.get("requires_approval") and not (assessment or {}).get("forced_escalation"):
        if assessment and assessment.get("auto_executed"):
            return ("Escalation Required:\n  no - low impact and high confidence, "
                    "executed autonomously")
        return "Escalation Required:\n  no"

    rows = ["  yes"]
    cost = float(plan.get("estimated_cost") or 0.0)
    if cost > APPROVAL_THRESHOLD:
        rows.append(f"  exceeds the {APPROVAL_THRESHOLD:,} approval threshold by "
                    f"{cost - APPROVAL_THRESHOLD:,.0f}")
    if assessment:
        rows.append(f"  impact {assessment['impact']} / confidence "
                    f"{assessment['confidence']}")
        for line in assessment.get("impact_because") or []:
            rows.append(f"    impact: {line}")
        for line in assessment.get("confidence_because") or []:
            rows.append(f"    confidence: {line}")
        for line in assessment.get("forced_escalation") or []:
            rows.append(f"    forced: {line}")
    elif state.get("approval_reason"):
        rows.append(f"  {state['approval_reason']}")
    return "Escalation Required:\n" + "\n".join(rows)


def _remaining(state: dict, plan: dict, assessment: dict | None) -> str:
    rows = []
    for a in plan.get("assumptions") or state.get("assumptions") or []:
        if a.get("expires_at"):
            rows.append(f"  {a['id']} expires {a['expires_at']}: {a['claim']}")
    for claim in state.get("claims") or []:
        if claim.get("status") == "CONTRADICTED":
            rows.append(f"  {claim['supplier_id']} is not trusted for this run; "
                        f"its units count as 0 confirmed")
        elif claim.get("status") == "UNVERIFIABLE":
            rows.append(f"  {claim['supplier_id']} claim \"{claim['claim']}\" could "
                        f"not be verified and is treated as absent")
    if int(state.get("tool_budget_remaining", 1)) <= 0:
        rows.append("  INCOMPLETE_INVESTIGATION: the tool budget ran out before "
                    "every question was answered")
    if not rows:
        rows.append("  none identified")
    rows.append("  next recheck: on the next environment change, or when the "
                "earliest assumption above expires")
    return "Remaining Risk:\n" + "\n".join(rows)


def main() -> None:
    """python -m output.brief <audit.jsonl> renders the brief a run produced,
    reconstructed from its audit trail."""
    import json
    import sys

    from output.audit import read_jsonl

    if len(sys.argv) < 2:
        print("usage: python -m output.brief <audit.jsonl>", file=sys.stderr)
        raise SystemExit(2)

    events = read_jsonl(sys.argv[1])
    state: dict = {"tools_called": [], "claims": [], "quotes": [],
                   "replies_received": [], "rejected_alternatives": []}
    for e in events:
        d = e.get("detail") or {}
        if e["type"] == "disruption_detected":
            state |= {"disruption_type": d.get("disruption_type"),
                      "severity": d.get("severity"),
                      "affected_component": d.get("component_id")}
        elif e["type"] == "calculation" and "baseline" in d:
            state["baseline"] = d["baseline"]
        elif e["type"] == "calculation" and "coverage_days" in d:
            state |= {"coverage_days": d["coverage_days"],
                      "usable_stock": d.get("usable_stock"),
                      "at_risk_orders": d.get("at_risk_orders")}
        elif e["type"] == "tool_call":
            state["tools_called"].append({"endpoint": d.get("endpoint"),
                                          "necessity": e.get("necessity"),
                                          "served_from_cache": d.get("served_from_cache")})
        elif e["type"] == "plan_proposed":
            state["plan"] = {**d, "estimated_cost": d.get("total_cost")}
        if e.get("alternatives_rejected"):
            state["rejected_alternatives"] = e["alternatives_rejected"]
    print(render_brief(state))


if __name__ == "__main__":
    main()
