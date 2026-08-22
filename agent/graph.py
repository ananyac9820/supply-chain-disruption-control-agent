"""The LangGraph state machine — Track B §4.1.

Six nodes, four conditional edges. LangGraph checkpoints AgentState on every
node transition, which is what makes interrupt() resumption and replanning
cheap: node 3 is re-entered with everything already known rather than
re-derived.

    START -> 1 monitor -> 2 impact -> 3 investigate -> 4 plan -> 5 gate -> 6 execute -> END

    (a) 3 investigate --[tool budget exhausted, G10]--> 5 gate      fail closed
    (b) 4 plan        --[validator veto, < 2 rounds]--> 4 plan      re-solve
        4 plan        --[G8 expired quote, < 3]-----> 3 investigate re-RFQ
    (c) 5 gate        --[approve | auto]-------------> 6 execute
        5 gate        --[edit]--------------------->   4 plan
        5 gate        --[reject]------------------->   END
    (d) 6 execute     --[assumption broke, < 3]----->  3 investigate replan

HOLLOW PASS: every node is a stub that writes its expected AgentState keys and
returns. The wiring and the routing conditions are real; the logic is not.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from agent.integrations import GUARDRAILS_AVAILABLE, validator_vetoed
from agent.nodes import execute, gate, impact, investigate, monitor, plan
from agent.nodes.plan import MAX_CORRECTION_ROUNDS
from contracts.constants import TOOL_BUDGET_PER_DISRUPTION
from contracts.state import AgentState

MAX_REPLANS = 3        # §4.5 — an agent that replans forever looks worse
                       # than one that asks for help

# A failed verdict is not the same thing as a veto. Some rules fail a plan for
# execution but re-solving under them returns the identical plan, so treating
# them as vetoes burns a correction round for nothing and delays the escalation
# the rule was asking for.
#
#   re-solve helps    G1  over budget      -> solve again under a tighter cap
#                     G5  safety stock     -> solve again with the reserve held
#                     G11 arrival past deadline -> solve again for that order
#
#   re-solve is futile  G2  cost over 150,000 -> the solver already minimised
#                           cost; the second solve returns the same number.
#                           G2 wants a human, not another attempt.
#                       G12 no feasible plan after the full relaxation ladder
#                       G10 tool budget exhausted
#
# G3, G4, G6 and G7 are pre-solve filters or model constraints and must be
# impossible to violate rather than checked afterwards, so they should never
# appear in a post-solve verdict at all.
#
# G8 (quote expired) is a THIRD kind, and neither of the other two fits. A
# stale quote is not fixed by solving again — the solver would re-use the same
# expired number — and it is not a reason to wake a human either. It is fixed
# by asking for a fresh quote, which is investigation. So it routes back to
# node 3, capped by the same replan_count as an assumption break so a supplier
# who keeps issuing short-lived quotes cannot spin the graph.
#
# Track A's guardrails/validator.py ships its own vetoed(), and theirs treats
# G8 as re-solvable. Ours is the routing decision rather than the rule
# semantics, so the G8 check runs first and their function decides the rest.
RESOLVABLE = frozenset({"G1", "G5", "G11"})
ESCALATE_ONLY = frozenset({"G2", "G10", "G12"})
NEEDS_FRESH_QUOTES = frozenset({"G8"})


def needs_fresh_quotes(verdict: dict) -> bool:
    """G8: the plan rests on a quote that has expired. Re-RFQ, do not re-solve."""
    return bool(set(verdict.get("fired") or []) & NEEDS_FRESH_QUOTES)


def vetoed(verdict: dict) -> bool:
    """True only when re-solving could actually change the answer.

    Track A's validator is the authority on its own rules, so use their
    vetoed() when it is importable and fall back to the table above before the
    merge. Either way G8 is handled by the caller, not here.
    """
    if verdict.get("passed", True):
        return False
    if GUARDRAILS_AVAILABLE and validator_vetoed is not None:
        from contracts.models import Verdict
        return bool(validator_vetoed(Verdict(**{
            "passed": verdict.get("passed", False),
            "fired": [f for f in (verdict.get("fired") or [])
                      if f not in NEEDS_FRESH_QUOTES],
            "reasons": verdict.get("reasons") or [],
            "forced_escalation": verdict.get("forced_escalation", False),
        })))
    if verdict.get("forced_escalation"):
        return False
    return bool(set(verdict.get("fired") or []) & RESOLVABLE)


# --------------------------------------------------------------- routing

def route_after_investigate(state: AgentState) -> str:
    """(a) G10 fails closed to escalation, never to a retry loop (§4.2)."""
    if int(state.get("tool_budget_remaining", TOOL_BUDGET_PER_DISRUPTION)) <= 0:
        return "gate"                       # flagged INCOMPLETE_INVESTIGATION
    return "plan"


def route_after_plan(state: AgentState) -> str:
    """(b) Three outcomes, not two.

    G8    -> investigate   the plan rests on an expired quote. A re-solve would
                           re-use the same stale number; only a fresh RFQ fixes
                           it, and RFQs are node 3's work. Capped by
                           replan_count so it cannot loop.
    veto  -> plan          G1, G5 or G11: solve again under the reason.
    else  -> gate          passed, or failed in a way re-solving cannot fix
                           (G2, G12), with both correction rounds unspent.
    """
    verdicts = state.get("guardrail_verdicts") or []
    latest = verdicts[-1] if verdicts else {}

    if needs_fresh_quotes(latest):
        if int(state.get("replan_count", 0)) < MAX_REPLANS:
            return "investigate"
        return "gate"                       # out of re-RFQ attempts -> escalate

    if not vetoed(latest):
        return "gate"                       # passed, or failed unresolvably
    rounds = int((state.get("plan") or {}).get("correction_rounds", 0))
    if rounds < MAX_CORRECTION_ROUNDS:
        return "plan"                       # re-solve under the veto reason
    return "gate"                           # out of rounds -> escalate


def route_after_gate(state: AgentState) -> str:
    """(c) Auto-execute, or the coordinator's answer to interrupt()."""
    decision = (state.get("human_response") or {}).get("decision", "auto")
    if decision in ("auto", "approve"):
        return "execute"
    if decision == "edit":
        return "plan"                       # re-solve under the human's edit
    return "end"                            # reject: stop, do not execute


def route_after_execute(state: AgentState) -> str:
    """(d) A broken assumption routes back into node 3, capped at MAX_REPLANS.
    The reopened disruption keeps its original disruption_id so the audit
    trail stays one narrative rather than two (§4.5)."""
    if state.get("broken_assumption") and int(state.get("replan_count", 0)) < MAX_REPLANS:
        return "investigate"
    return "end"


# ----------------------------------------------------------------- build

def build_graph(checkpointer=None):
    """Compile the six-node graph. A checkpointer is required for interrupt()
    and Command(resume=...) to resume from the checkpoint rather than restart."""
    g = StateGraph(AgentState)

    g.add_node("monitor", monitor)          # 1
    g.add_node("impact", impact)            # 2
    g.add_node("investigate", investigate)  # 3
    g.add_node("plan", plan)                # 4
    g.add_node("gate", gate)                # 5
    g.add_node("execute", execute)          # 6

    g.add_edge(START, "monitor")
    g.add_edge("monitor", "impact")
    g.add_edge("impact", "investigate")

    g.add_conditional_edges("investigate", route_after_investigate,
                            {"plan": "plan", "gate": "gate"})
    g.add_conditional_edges("plan", route_after_plan,
                            {"plan": "plan", "gate": "gate",
                             "investigate": "investigate"})
    g.add_conditional_edges("gate", route_after_gate,
                            {"execute": "execute", "plan": "plan", "end": END})
    g.add_conditional_edges("execute", route_after_execute,
                            {"investigate": "investigate", "end": END})

    return g.compile(checkpointer=checkpointer or InMemorySaver())


def initial_state(disruption_id: str = "DIS-001") -> AgentState:
    """Every key present from the first transition. reserved_budget and
    reserved_stock are here from hour 2 even though the multi-disruption
    arbiter is a stretch goal — present from the start it is a two-hour add
    at hour 17, absent it is impossible."""
    return {
        "disruption_id": disruption_id,
        "disruption_type": "",
        "severity": "low",
        "affected_component": "",
        "usable_stock": 0,
        "coverage_days": 0.0,
        "at_risk_orders": [],
        "baseline": {},
        "tools_called": [],
        "tool_budget_remaining": TOOL_BUDGET_PER_DISRUPTION,
        "messages_sent": [],
        "replies_received": [],
        "claims": [],
        "quotes": [],
        "plan": None,
        "assumptions": [],
        "rejected_alternatives": [],
        "guardrail_verdicts": [],
        "replan_count": 0,
        "broken_assumption": None,
        "reserved_budget": 0.0,
        "reserved_stock": {},
        "requires_approval": False,
        "approval_reason": None,
        "human_response": None,
        "erp_writes": [],
        "audit_events": [],
    }
