"""Supplier trust — two axes, deliberately separate.

    reputation(supplier_id)      slow, historical, about a counterparty
    shipment_confidence(po_id)   fast, per-shipment, about one consignment

They answer different questions and must not be collapsed into one number.

"We cannot confirm these units arrived" is an observation about a shipment.
"This supplier is unreliable" is a claim about an organisation. A dispatch
claim standing against `label_created_no_pickup` supports the first and not
the second: the supplier may have packed and tendered on time while the
courier never collected. Punishing the supplier for that is both unfair and
wrong on the facts, and a reputation number is sticky — it goes on affecting
every future decision long after the shipment is resolved.

So: every discrepancy is recorded as an incident and drops that shipment's
confidence, whatever the cause. Only an incident that evidence attributes to
SUPPLIER moves reputation. Attribution happens in sandbox/attribution.py,
which owns the tracking evidence.

Reputation still has to change decisions to be worth keeping — it feeds
effective_reliability and therefore the solver's risk term, weighted by
W_RISK from contracts.constants so both solvers move together. It just has to
change them for a reason the evidence supports.
"""

from typing import Literal

from pydantic import BaseModel

from sandbox import db

TrustEvent = Literal["on_time", "late", "contradicted_claim",
                     "moq_failure", "quality_miss"]

# §4.5's penalty weights. An unsupported dispatch claim that the evidence
# attributes to the supplier weighs three times a late delivery: a delivery
# can slip for reasons outside anyone's control, whereas a consignment that
# was never packed is a failure of the commitment itself.
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


# Shipment confidence starts here and falls on any verified discrepancy,
# whoever turns out to be responsible.
CONFIDENCE_FULL = 1.0
DISCREPANCY_PENALTY = 0.6
# Below this, in-transit units on that PO are not counted as confirmed.
UNCONFIRMED_BELOW = 0.5


class SupplierTrust(BaseModel):
    supplier_id: str
    on_time_count: int = 0
    late_count: int = 0
    contradicted_claims: int = 0
    moq_failures: int = 0
    quality_delta: float = 0.0

    @property
    def penalty(self) -> float:
        """How far this history pulls a catalog reliability score down."""
        return (PENALTY_CONTRADICTED * self.contradicted_claims
                + PENALTY_LATE * self.late_count
                + PENALTY_MOQ_FAILURE * self.moq_failures)


def trust_read(supplier_id: str) -> SupplierTrust:
    """A supplier with no history reads as a clean slate, not an error."""
    with db.session() as conn:
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

    with db.session() as conn:
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


def reputation(supplier_id: str) -> SupplierTrust:
    """The slow axis: what this counterparty's history says about it.

    Only incidents attributed to SUPPLIER reach this, plus the explicitly
    recorded outcomes (late, moq_failure, quality_miss). An UNATTRIBUTED or
    COURIER incident leaves it untouched, however inconvenient the shipment
    was, because the evidence does not support moving it.

    Same underlying row as trust_read; this is the name to use when you mean
    the reputation axis rather than the raw counters.
    """
    return trust_read(supplier_id)


def effective_reliability(supplier_id: str, catalog_score: float) -> float:
    """The catalog score, penalised by attributed history. Signature frozen.

    §4.5's formula exactly. quality_delta is recorded by trust_write and shown
    in the brief, but is deliberately absent from the penalty: a quality miss
    already removes a supplier through G4's floor, and charging it twice would
    double-count one failure.
    """
    return max(RELIABILITY_FLOOR, catalog_score - reputation(supplier_id).penalty)


def shipment_confidence(po_id: str) -> float:
    """The fast axis: can we count this consignment's units as arrived?

    Falls on every verified discrepancy on that PO, regardless of who is
    responsible — an unattributed shipment is exactly as unverifiable as one
    that turns out to be the supplier's doing. Reputation stays out of it.
    """
    with db.session() as conn:
        incidents = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE po_id = ?", (po_id,)
        ).fetchone()[0]
    return max(0.0, CONFIDENCE_FULL - DISCREPANCY_PENALTY * incidents)


def units_confirmed(po_id: str) -> bool:
    """Whether in-transit units on this PO may be counted toward coverage."""
    return shipment_confidence(po_id) >= UNCONFIRMED_BELOW


def record_incident(po_id: str, supplier_id: str, observed: str, expected: str,
                    attribution: str, attribution_basis: str,
                    ts: "datetime | None" = None) -> str | None:
    """Log one discrepancy. Always logged; reputation moves only if attributed.

    Idempotent per (po_id, observed): re-reading tracking does not compound
    either axis, so a read cache changes cost and not answers.
    """
    from sandbox import db as _db          # local: trust.py is imported by demo code
    from sandbox.attribution import ATTRIBUTIONS

    if attribution not in ATTRIBUTIONS:
        raise ValueError(f"unknown attribution: {attribution}")

    stamp = (ts or _db.sim_now()).isoformat()
    with db.session() as conn:
        existing = conn.execute(
            "SELECT 1 FROM incidents WHERE po_id = ? AND observed = ?",
            (po_id, observed)).fetchone()
        if existing:
            return None
        n = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        incident_id = f"INC-{n + 1:04d}"
        conn.execute(
            "INSERT INTO incidents (incident_id, po_id, supplier_id, observed,"
            " expected, attribution, attribution_basis, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (incident_id, po_id, supplier_id, observed, expected,
             attribution, attribution_basis, stamp))

    # The whole point of the split: only an attributed incident is allowed to
    # follow a supplier into its next quote.
    if attribution == "SUPPLIER":
        trust_write(supplier_id, "contradicted_claim")
    return incident_id


def incidents_for(po_id: str | None = None,
                  supplier_id: str | None = None) -> list[dict]:
    """The incident log, for the audit trail and the decision brief."""
    sql = "SELECT * FROM incidents"
    where, args = [], []
    if po_id:
        where.append("po_id = ?"); args.append(po_id)
    if supplier_id:
        where.append("supplier_id = ?"); args.append(supplier_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    with db.session() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY incident_id", args)]


def trust_reset(supplier_id: str | None = None) -> None:
    """Clear the reputation ledger. Reset recreates the database; this is for
    tests that need a clean slate without one."""
    with db.session() as conn:
        if supplier_id is None:
            conn.execute("DELETE FROM supplier_trust")
        else:
            conn.execute("DELETE FROM supplier_trust WHERE supplier_id = ?",
                         (supplier_id,))
