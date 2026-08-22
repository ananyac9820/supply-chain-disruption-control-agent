"""The LangGraph state machine — Track B §4.1.

Six nodes, four conditional edges. LangGraph checkpoints AgentState on every
node transition, which is what makes interrupt() resumption and replanning
cheap: node 3 is re-entered with everything already known rather than
re-derived.

    START -> 1 monitor -> 2 impact -> 3 investigate -> 4 plan -> 5 gate -> 6 execute -> END

    (a) 3 investigate --[tool budget exhausted, G10]--> 5 gate      fail closed
    (b) 4 plan        --[validator veto, < 2 rounds]--> 4 plan      re-solve
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
# G8 (quote expired) is neither: it needs a fresh RFQ, which is node 3's work,
# not another solve. Escalating is the safe reading until node 4 lands at hour
# 10.5 and can route it back to investigation.
RESOLVABLE = frozenset({"G1", "G5", "G11"})
ESCALATE_ONLY = frozenset({"G2", "G8", "G10", "G12"})


def vetoed(verdict: dict) -> bool:
    """True only when re-solving could actually change the answer."""
    if verdict.get("passed", True):
        return False
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
    """(b) The validator vetoes, the planner re-solves. The LLM does not get to
    overrule the validator; after MAX_CORRECTION_ROUNDS it escalates.

    Branches on vetoed(), not on passed: a G2 or G12 firing fails the plan for
    execution but re-solving under it is futile, so it goes straight to the
    gate with both correction rounds still unspent."""
    verdicts = state.get("guardrail_verdicts") or []
    latest = verdicts[-1] if verdicts else {}
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
                            {"plan": "plan", "gate": "gate"})
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
