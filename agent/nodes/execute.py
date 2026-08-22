"""Node 6 — execute. ERP writes, the decision brief, the audit flush.

Writes: erp_writes, assumptions, broken_assumption, audit_events

THE SIX ACTIONS, AND ONLY THE SIX
PS §5.9 permits exactly six ERP actions and the sandbox rejects anything else
with a 400. They are not a suggestion and there is no seventh:

    mark_po_delayed          the disrupted PO, so the ERP stops expecting it
    create_alternate_po      one per allocation the solver chose
    attach_supplier_note     what verification found, against the supplier
    update_production_risk   per production order the plan reschedules or leaves
                             exposed
    record_escalation        when a human was asked, and what they answered
    store_recovery_plan      the plan itself, so the decision is retrievable

Every write goes through agent/tools.py like every other sandbox call, so an
ERP write is metered and appears in the trail with its necessity.

THEN THE ASSUMPTIONS
The plan is only adopted once it has executed, so this is where its assumptions
are registered (§4.4) and where the watcher first runs. If one has already
broken - the environment moved while the coordinator was deciding - the run
routes straight back into node 3 with broken_assumption set, and the
assumption_break event is on the trail before node 3 makes any LLM call.

REPLAN HYGIENE (§4.5)
The units this node commits stay committed. On a replan node 4 re-solves for
the shortfall only, reading the commitments recorded here; verified quotes are
preserved and only what actually changed is re-fetched. A reopened disruption
keeps its original disruption_id, so the audit trail stays one narrative rather
than two.
"""

from __future__ import annotations

from agent.assumptions import break_summary, register, watch
from agent.audit import append_event
from agent.integrations import status
from agent.tools import call_tool
from contracts.state import AgentState

MAX_REPLANS = 3         # mirrors agent/graph.py; §4.5


def execute(state: AgentState) -> dict:
    work = dict(state)
    plan = work.get("plan") or {}
    human = work.get("human_response") or {}
    erp_writes = list(work.get("erp_writes") or [])

    executed = plan.get("status") in ("OPTIMAL", "FEASIBLE")
    # Idempotent per plan_id. Node 6 is re-entered after a replan, and the run
    # must post the NEW plan rather than a second copy of the old one -
    # duplicate create_alternate_po writes would double the committed units and
    # make the next re-solve think the suppliers were exhausted.
    already = {w.get("payload", {}).get("plan_id") for w in erp_writes
               if w.get("action") == "store_recovery_plan"}
    adopted = executed          # an adopted plan is watched every time
    if executed and plan.get("plan_id") in already:
        executed = False
        work["audit_events"] = append_event(
            work, type="erp_update", actor="executor",
            summary=f"{plan.get('plan_id')} already written; no duplicate ERP writes",
            detail={"plan_id": plan.get("plan_id"),
                    "rationale": "node 6 is idempotent per plan so a replan does "
                                 "not double the committed units"})
    elif executed:
        erp_writes += _write_plan(work, plan, human)
    else:
        work["audit_events"] = append_event(
            work, type="erp_update", actor="executor",
            summary=f"no ERP writes: plan status {plan.get('status')}",
            detail={"status": plan.get("status"),
                    "binding_constraint": plan.get("binding_constraint"),
                    "rationale": "nothing is written for a plan that cannot "
                                 "execute; the escalation carries the reason"})

    # The brief. Deterministic renderer, no model call - node 4 already wrote
    # the rationale it embeds.
    from output.brief import render_brief
    brief = render_brief(work)
    work["audit_events"] = append_event(
        work, type="decision", actor="executor",
        summary=(f"decision brief rendered for {plan.get('plan_id', 'the plan')} "
                 f"({'executed' if executed else 'not executed'})"),
        detail={"brief": brief, "decision": human.get("decision"),
                "rationale": plan.get("rationale")},
        baseline_delta=_delta(work, plan))

    # Register what the plan now depends on, then look once.
    assumptions = register(work, plan) if adopted else list(work.get("assumptions") or [])
    work["assumptions"] = assumptions
    broken = watch(work, assumptions) if adopted else []

    out: dict = {
        "erp_writes": erp_writes,
        "assumptions": assumptions,
        "broken_assumption": None,
        "replan_count": int(work.get("replan_count", 0)),
        "tools_called": work["tools_called"],
        "tool_budget_remaining": work["tool_budget_remaining"],
    }

    if broken and int(work.get("replan_count", 0)) < MAX_REPLANS:
        first = broken[0]
        out["broken_assumption"] = f"{first['id']}: {first['reason']}"
        out["replan_count"] = int(work.get("replan_count", 0)) + 1
        # BEFORE any LLM call: node 3 is the next node and its first act is a
        # model call, so this event has to be on the trail now.
        work["audit_events"] = append_event(
            work, type="assumption_break", actor="watcher",
            summary=break_summary(broken),
            detail={"broken": broken, "trigger": first["trigger"],
                    "assumption_id": first["id"], "expected": first["expected"],
                    "observed": first["observed"],
                    "replan_count": out["replan_count"],
                    "rationale": "detected by comparing a recorded value against a "
                                 "re-read one, not by asking a model whether "
                                 "anything looked different"},
            remaining_risk=first["reason"])
        out["audit_events"] = work["audit_events"]
        return out

    if broken:
        work["audit_events"] = append_event(
            work, type="escalation", actor="watcher",
            summary=(f"{len(broken)} assumption(s) broken but the replan cap of "
                     f"{MAX_REPLANS} is reached - escalating instead"),
            detail={"broken": broken, "replan_count": work.get("replan_count"),
                    "rationale": "an agent that replans forever looks worse than "
                                 "one that asks for help"},
            remaining_risk="; ".join(b["reason"] for b in broken))

    out["audit_events"] = append_event(
        work, type="run_complete", actor="executor",
        summary=(f"run complete - {len(erp_writes)} ERP writes, "
                 f"{len(assumptions)} assumptions registered"),
        detail={"executed": adopted, "erp_writes": erp_writes,
                "assumptions": assumptions,
                "decision": human.get("decision"),
                "track_a_integrations": status()},
        baseline_delta=_delta(work, plan),
        remaining_risk=_remaining_risk(work, plan, assumptions))
    return out


# ---- the six actions ---------------------------------------------------

def _write_plan(work: dict, plan: dict, human: dict) -> list[dict]:
    """Every ERP write this plan implies, in the order an operator would make
    them: stop expecting the late PO, raise the replacements, record what we
    learned about the supplier, flag the production risk, log the escalation,
    store the plan."""
    writes: list[dict] = []
    component_id = work.get("affected_component") or ""

    po_id = _disrupted_po(work)
    if po_id:
        writes.append(_erp(work, "mark_po_delayed",
                           {"po_id": po_id, "reason": work.get("disruption_type")},
                           "the ERP must stop counting the late PO as incoming "
                           "stock or every downstream figure is wrong"))

    for alloc in plan.get("allocations") or []:
        quote = next((q for q in work.get("quotes") or []
                      if q["supplier_id"] == alloc["supplier_id"]), None)
        writes.append(_erp(work, "create_alternate_po", {
            "component_id": component_id,
            "supplier_id": alloc["supplier_id"],
            "quantity": alloc["units"],
            "unit_price": (quote or {}).get("unit_price"),
            "arrival_day": alloc["arrival_day"],
            "total_value": alloc["cost"],
        }, f"the plan commits {alloc['units']} units from "
           f"{alloc['supplier_id']}; the commitment has to exist in the ERP"))

    for claim in work.get("claims") or []:
        if claim.get("status") in ("CONTRADICTED", "UNVERIFIABLE"):
            writes.append(_erp(work, "attach_supplier_note", {
                "supplier_id": claim["supplier_id"],
                "note": (f"claim \"{claim['claim']}\" was {claim['status']} "
                         f"against tracking on {work.get('disruption_id')}"),
                "evidence": claim.get("evidence"),
            }, "what verification found has to outlive this run or the next "
               "planner starts from the same false premise"))

    rescheduled = {r["production_order_id"]: r["delay_days"]
                   for r in plan.get("reschedules") or []}
    for pid in (work.get("at_risk_orders") or []):
        writes.append(_erp(work, "update_production_risk", {
            "production_order_id": pid,
            "delay_days": rescheduled.get(pid, 0),
            "risk": "rescheduled" if pid in rescheduled else "covered_by_plan",
        }, "production planning needs the risk on the order, not buried in a "
           "procurement decision"))

    if work.get("requires_approval"):
        writes.append(_erp(work, "record_escalation", {
            "disruption_id": work.get("disruption_id"),
            "reason": work.get("approval_reason"),
            "decision": human.get("decision"),
            "estimated_cost": plan.get("estimated_cost"),
        }, "an approval that is not recorded did not happen as far as an "
           "auditor is concerned"))

    writes.append(_erp(work, "store_recovery_plan", {
        "disruption_id": work.get("disruption_id"),
        "plan_id": plan.get("plan_id"),
        "status": plan.get("status"),
        "total_cost": plan.get("total_cost"),
        "allocations": plan.get("allocations"),
        "reschedules": plan.get("reschedules"),
        "rationale": plan.get("rationale"),
    }, "the decision itself has to be retrievable later, not reconstructed "
       "from its side effects"))
    return writes


def _erp(work: dict, action: str, payload: dict, necessity: str) -> dict:
    """One metered POST /erp/update. The sandbox rejects any action outside the
    six PS §5.9 permits, so a rejection here is a bug in this file, not a
    transient failure - it is recorded rather than retried."""
    result = call_tool(work, "erp_update", necessity, phase="execution",
                       action=action, payload=payload)
    record = {"action": action, "payload": payload, "result": result}
    work["audit_events"] = append_event(
        work, type="erp_update", actor="executor",
        summary=f"{action} -> {result.get('status')} {result.get('record_id') or ''}".strip(),
        detail={**record, "rationale": necessity},
        tools_used=["POST /erp/update"], necessity=necessity)
    return record


def _disrupted_po(work: dict) -> str | None:
    for call in work.get("tools_called") or []:
        endpoint = call.get("endpoint") or ""
        if endpoint.startswith("GET /tracking/"):
            return endpoint.rsplit("/", 1)[-1]
    return None


# ---- reporting ---------------------------------------------------------

def _delta(work: dict, plan: dict) -> dict | None:
    base = work.get("baseline") or {}
    if not base or plan.get("total_cost") is None:
        return None
    inaction = float(base.get("cost_of_inaction") or 0.0)
    return {"cost_of_inaction": inaction, "plan_cost": plan.get("total_cost"),
            "net_avoided": round(inaction - float(plan.get("total_cost") or 0.0), 2),
            "production_days_recovered": base.get("production_days_lost")}


def _remaining_risk(work: dict, plan: dict, assumptions: list[dict]) -> str | None:
    """The last event on the trail carries the remaining risk (PS §4.10)."""
    bits = []
    expiring = [a for a in assumptions if a.get("expires_at")]
    if expiring:
        soonest = min(expiring, key=lambda a: a["expires_at"])
        bits.append(f"{soonest['id']} expires {soonest['expires_at']}: "
                    f"{soonest['claim']}")
    for claim in work.get("claims") or []:
        if claim.get("status") == "CONTRADICTED":
            bits.append(f"{claim['supplier_id']} remains untrusted for this run")
    if int(work.get("tool_budget_remaining", 1)) <= 0:
        bits.append("INCOMPLETE_INVESTIGATION: the tool budget ran out")
    if not bits:
        bits.append("no open risk identified; assumptions are re-checked on the "
                    "next environment change")
    return "; ".join(bits)
