"""The graph's bridge to output/audit.py.

Nodes do not accumulate audit events in AgentState. They emit them through the
real writer, which validates each one against contracts/audit_schema.py and
appends it to the disruption's JSONL immediately. Two reasons:

  1. LangGraph checkpoints AgentState on every node transition. Carrying the
     full event bodies there re-serialises the whole trail once per transition,
     and it puts the audit trail's durability at the mercy of the checkpointer
     rather than of a flushed file.
  2. The file is the deliverable. PS §4.10 is graded from the JSONL, and
     `python -m output.cli <path> --follow` can tail a run only if the lines
     are on disk while the run is still going.

state["audit_events"] is still populated, because the key is part of the frozen
AgentState — but it holds a compact index (event_id, type, summary) rather than
the event bodies. That is enough for a node to reason about what has already
been recorded and enough for a test to assert ordering, while the full record
lives in the file. The annotation is list[dict] either way, and Track A never
reads this key.

The registry is keyed by disruption_id so a reopened disruption keeps writing
to its original file: a replanned run stays one narrative rather than two.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from contracts.stub_sandbox import NOW
from output.audit import AuditLog

_LOGS: dict[str, AuditLog] = {}


def open_log(disruption_id: str, path: str | Path | None = None,
             log_dir: str | Path | None = None) -> AuditLog:
    """Open (or reopen) the log for a disruption. agent/run.py calls this once
    before invoking the graph."""
    close_log(disruption_id)
    log = AuditLog(disruption_id, path=path, log_dir=log_dir)
    _LOGS[disruption_id] = log
    return log


def get_log(disruption_id: str) -> AuditLog:
    """The log a node writes through. Created on demand so a node stays usable
    in a unit test without ceremony."""
    log = _LOGS.get(disruption_id)
    if log is None:
        log = AuditLog(disruption_id)
        _LOGS[disruption_id] = log
    return log


def close_log(disruption_id: str) -> None:
    log = _LOGS.pop(disruption_id, None)
    if log is not None:
        log.close()


def log_path(disruption_id: str) -> Path:
    return get_log(disruption_id).path


def append_event(state, *, type: str, actor: str, summary: str,
                 detail: dict | None = None, tools_used: list[str] | None = None,
                 necessity: str | None = None,
                 alternatives_rejected: list[dict] | None = None,
                 baseline_delta: dict | None = None,
                 remaining_risk: str | None = None) -> list[dict]:
    """Write one validated event to the JSONL, return the updated compact index
    for state["audit_events"]."""
    disruption_id = state.get("disruption_id") or "DIS-000"
    record = get_log(disruption_id).emit(
        ts=_ts(), type=type, actor=actor, summary=summary, detail=detail,
        tools_used=tools_used, necessity=necessity,
        alternatives_rejected=alternatives_rejected,
        baseline_delta=baseline_delta, remaining_risk=remaining_risk,
    )
    index = list(state.get("audit_events") or [])
    index.append({"event_id": record["event_id"], "type": record["type"],
                  "summary": record["summary"]})
    return index


def _ts() -> datetime:
    """Simulated clock. The stub sandbox is frozen at NOW so canned records stay
    deterministic; real wall-clock arrives with HttpSandbox at hour 12."""
    return NOW
