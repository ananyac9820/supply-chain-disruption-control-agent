"""Shared agent state — Track A §3.2 / master plan §8.7.

Track A never writes this file's contents at runtime; it exists here so the
shape is frozen for both tracks. LangGraph checkpoints AgentState on every
node transition, which is what makes interrupt() resumption and D-2
replanning work without re-deriving anything.

FROZEN AT HOUR 1.5.
"""

from typing import TypedDict, Literal


class AgentState(TypedDict):
    disruption_id: str
    disruption_type: str
    severity: Literal["low","medium","high","critical"]
    affected_component: str
    # node 2 — deterministic
    usable_stock: int
    coverage_days: float
    at_risk_orders: list[str]
    baseline: dict          # {units_short, production_days_lost, deadline_misses, cost_of_inaction}
    # node 3 — investigation
    tools_called: list[dict]
    tool_budget_remaining: int
    messages_sent: list[dict]
    replies_received: list[dict]
    claims: list[dict]      # {supplier_id, claim, status, evidence}
    quotes: list[dict]
    # node 4 — planning
    plan: dict | None
    assumptions: list[dict]
    rejected_alternatives: list[dict]
    guardrail_verdicts: list[dict]
    replan_count: int
    broken_assumption: str | None
    # shared across concurrent disruptions
    reserved_budget: float
    reserved_stock: dict
    # nodes 5 and 6
    requires_approval: bool
    approval_reason: str | None
    human_response: dict | None
    erp_writes: list[dict]
    audit_events: list[dict]
