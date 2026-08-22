"""Minimal audit-event helper for the hollow pass.

output/audit.py (the real JSONL writer, hour 3.5) replaces the persistence
half of this. The append helper stays here because AgentState is frozen with
plain `list[dict]` fields and no LangGraph reducer — so a node that wants to
add an event must read the current list and return the extended one. Every
node does that through append_event() rather than open-coding it.
"""

from __future__ import annotations

from datetime import datetime

from contracts.audit_schema import AuditEvent
from contracts.stub_sandbox import NOW


def append_event(state, *, type: str, actor: str, summary: str,
                 detail: dict | None = None, tools_used: list[str] | None = None,
                 necessity: str | None = None,
                 alternatives_rejected: list[dict] | None = None,
                 baseline_delta: dict | None = None,
                 remaining_risk: str | None = None) -> list[dict]:
    """Return state's audit_events extended by one validated event."""
    events = list(state.get("audit_events") or [])
    event = AuditEvent(
        event_id=f"EV-{len(events) + 1:04d}",
        disruption_id=state.get("disruption_id") or "DIS-000",
        ts=_ts(),
        type=type,
        actor=actor,
        summary=summary,
        detail=detail or {},
        tools_used=tools_used or [],
        necessity=necessity,
        alternatives_rejected=alternatives_rejected or [],
        baseline_delta=baseline_delta,
        remaining_risk=remaining_risk,
    )
    events.append(event.model_dump(mode="json"))
    return events


def _ts() -> datetime:
    """Simulated clock. The stub sandbox is frozen at NOW so canned records
    stay deterministic; real wall-clock arrives with HttpSandbox at hour 12."""
    return NOW
