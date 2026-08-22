"""The audit JSONL writer — one file per disruption, one AuditEvent per line.

PS §4.10 requires ten things of an audit trail. Where each lives:

    detected disruption        opening disruption_detected event
    data sources checked       tools_used across all events
    supplier messages          tool_call events on /suppliers/{id}/message
                               plus inbox reads
    alternatives considered    alternatives_rejected
    calculations performed     type: "calculation" events
    decision made              type: "decision" event, summary
    reason for decision        detail.rationale
    ERP updates made           type: "erp_update" events
    escalations triggered      type: "escalation" events
    remaining risks            remaining_risk on the final event

Every line is validated against contracts/audit_schema.py before it is
written, so a malformed event fails here rather than in front of a judge.

Lines are flushed on write, which is what lets `python -m output.cli --follow`
tail a run as it happens.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from contracts.audit_schema import AuditEvent

DEFAULT_LOG_DIR = Path(os.environ.get("SCDA_AUDIT_DIR", "audit_logs"))


class AuditLog:
    """An append-only JSONL log for one disruption.

    Event ids are assigned here, not by the caller, so they are dense and
    ordered no matter which node emits them.
    """

    def __init__(self, disruption_id: str, path: str | Path | None = None,
                 log_dir: str | Path | None = None) -> None:
        self.disruption_id = disruption_id
        base = Path(log_dir) if log_dir is not None else DEFAULT_LOG_DIR
        self.path = Path(path) if path is not None else base / f"{disruption_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._fh = self.path.open("w", encoding="utf-8", newline="\n")

    # ---- writing -----------------------------------------------------

    def emit(self, *, type: str, actor: str, summary: str,
             ts: datetime | None = None, detail: dict | None = None,
             tools_used: list[str] | None = None, necessity: str | None = None,
             alternatives_rejected: list[dict] | None = None,
             baseline_delta: dict | None = None,
             remaining_risk: str | None = None) -> dict:
        """Validate, write one line, return the event as a dict."""
        self._seq += 1
        event = AuditEvent(
            event_id=f"EV-{self._seq:04d}",
            disruption_id=self.disruption_id,
            ts=ts or datetime.now(),
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
        return self.write(event)

    def write(self, event: AuditEvent) -> dict:
        record = event.model_dump(mode="json")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()          # so --follow sees it immediately
        return record

    # ---- lifecycle ---------------------------------------------------

    @property
    def count(self) -> int:
        return self._seq

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---- reading (the CLI, the brief and the dashboard all use this) ------

def read_jsonl(path: str | Path) -> list[dict]:
    """Read a whole audit file. Every line is re-validated on the way in, so
    a renderer never has to defend against a half-written record."""
    return list(iter_jsonl(path))


def iter_jsonl(path: str | Path) -> Iterator[dict]:
    with Path(path).open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield AuditEvent.model_validate_json(line).model_dump(mode="json")
            except Exception as exc:      # noqa: BLE001 - reported, not raised
                raise ValueError(f"{path}:{lineno} is not a valid AuditEvent: {exc}") from exc
