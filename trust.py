"""Supplier trust ledger — §4.5.

One SQLite table and three functions. Not a service, not a class hierarchy.

The point of this file is not that it records supplier behaviour. It is that
the record changes the answer inside a single run: SUP-21 is caught in a false
dispatch claim at minute 3, its effective reliability drops, and when SUP-21
reappears as a quote candidate at minute 9 the solver ranks it lower because
of that. A ledger that never alters a decision is decoration.

The mechanism is the solver's risk term, weighted by W_RISK. Both solvers
import that weight from contracts.constants, so a change to the ledger's
influence cannot land in one and not the other.
"""

from typing import Literal

from pydantic import BaseModel

from sandbox import db

TrustEvent = Literal["on_time", "late", "contradicted_claim",
                     "moq_failure", "quality_miss"]

# §4.5's penalty weights. A contradicted claim costs three times what a late
# delivery does: being wrong is forgivable, being untruthful is not.
PENALTY_CONTRADICTED = 0.15
PENALTY_LATE = 0.05
PENALTY_MOQ_FAILURE = 0.05
RELIABILITY_FLOOR = 0.05

QUALITY_MISS_DELTA = 0.05

_COUNTER = {
    "on_time": "on_time_count",
    "late": "late_count",
    "contradicted_claim": "contradicted_claims",
    "moq_failure": "moq_failures",
}


class SupplierTrust(BaseModel):
    supplier_id: str
    on_time_count: int = 0
    late_count: int = 0
    contradicted_claims: int = 0
    moq_failures: int = 0
    quality_delta: float = 0.0


def trust_read(supplier_id: str) -> SupplierTrust:
    """A supplier with no history reads as a clean slate, not an error."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM supplier_trust WHERE supplier_id = ?",
                           (supplier_id,)).fetchone()
    if row is None:
        return SupplierTrust(supplier_id=supplier_id)
    return SupplierTrust(
        supplier_id=row["supplier_id"],
        on_time_count=row["on_time_count"],
        late_count=row["late_count"],
        contradicted_claims=row["contradicted_claims"],
        moq_failures=row["moq_failures"],
        quality_delta=row["quality_delta"],
    )


def trust_write(supplier_id: str, event: TrustEvent) -> None:
    """Record one observation. Idempotent only in the sense that it always
    appends — calling it twice for the same event counts it twice, which is
    correct: a supplier that lies twice is worse than one that lies once."""
    if event not in _COUNTER and event != "quality_miss":
        raise ValueError(f"unknown trust event: {event}")

    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO supplier_trust (supplier_id) VALUES (?)",
            (supplier_id,))
        if event == "quality_miss":
            conn.execute(
                "UPDATE supplier_trust SET quality_delta = quality_delta + ?"
                " WHERE supplier_id = ?", (QUALITY_MISS_DELTA, supplier_id))
        else:
            column = _COUNTER[event]
            conn.execute(
                f"UPDATE supplier_trust SET {column} = {column} + 1"
                " WHERE supplier_id = ?", (supplier_id,))


def effective_reliability(supplier_id: str, catalog_score: float) -> float:
    """The catalog score, penalised by what we have actually observed.

    §4.5's formula exactly. quality_delta is recorded by trust_write and shown
    in the brief, but is deliberately absent from the penalty: a quality miss
    already removes a supplier through G4's floor, and charging it twice would
    double-count one failure.
    """
    t = trust_read(supplier_id)
    penalty = (PENALTY_CONTRADICTED * t.contradicted_claims
               + PENALTY_LATE * t.late_count
               + PENALTY_MOQ_FAILURE * t.moq_failures)
    return max(RELIABILITY_FLOOR, catalog_score - penalty)


def trust_reset(supplier_id: str | None = None) -> None:
    """Clear the ledger. Used by POST /sim/reset and by tests, not in a run."""
    with db.connect() as conn:
        if supplier_id is None:
            conn.execute("DELETE FROM supplier_trust")
        else:
            conn.execute("DELETE FROM supplier_trust WHERE supplier_id = ?",
                         (supplier_id,))
