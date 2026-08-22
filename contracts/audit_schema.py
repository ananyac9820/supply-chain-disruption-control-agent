"""Audit event schema — Track A §3.3 / master plan §9.2.

Every field required by PS §4.10 maps to one of these. Track B writes
these; Track A never touches them.

FROZEN AT HOUR 1.5.
"""

from pydantic import BaseModel
from datetime import datetime
from typing import Literal


class AuditEvent(BaseModel):
    event_id: str                # "EV-0007"
    disruption_id: str           # "DIS-001"
    ts: datetime
    type: Literal["disruption_detected","tool_call","calculation","verification",
                  "decision","guardrail","escalation","erp_update","replan",
                  "assumption_break","plan_proposed","run_complete"]
    actor: str                   # "verification_agent"
    summary: str                 # one line, ops-manager readable
    detail: dict
    tools_used: list[str] = []
    necessity: str | None = None
    alternatives_rejected: list[dict] = []
    baseline_delta: dict | None = None
    remaining_risk: str | None = None
