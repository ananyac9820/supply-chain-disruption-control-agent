"""Import guards for the Track A surfaces Track B calls.

Track B calls solve(), validate() and the trust ledger; it never implements
them. Until Track A merges those folders into main the imports fail and the
flags go False, which lets the graph run end to end without Track B inventing
a solver, a rule set or a trust store.

At hour 12 all three imports resolve and nothing else in agent/ changes.

The trust fallback is the one place with observable behaviour rather than a
None: when a claim is contradicted the run must still be able to say what the
penalty was, because that number is the point of the Act-2 demo beat. So it
records the delta in-process, marked so no reader can mistake it for the real
ledger. It persists nothing and it is not a reimplementation of trust.py -
Person A's version owns the schema, the persistence and the arithmetic.
"""

from __future__ import annotations

try:                                    # Track A: solver/__init__.py re-exports
    from solver import solve            # fallback.solve now, CP-SAT at hour 12
    SOLVER_AVAILABLE = True
except ImportError:                     # pragma: no cover - pre-merge path
    solve = None                        # type: ignore[assignment]
    SOLVER_AVAILABLE = False

try:                                    # Track A: guardrails/validator.py
    from guardrails.validator import validate
    from guardrails.validator import vetoed as validator_vetoed
    GUARDRAILS_AVAILABLE = True
except ImportError:                     # pragma: no cover - pre-merge path
    validate = None                     # type: ignore[assignment]
    validator_vetoed = None             # type: ignore[assignment]
    GUARDRAILS_AVAILABLE = False

TRUST_DEGRADED = False

# Track A split trust into two axes and Track B must not collapse them back.
#
#   reputation(supplier_id)     slow, historical, about a counterparty.
#                               Moves ONLY on an incident the evidence
#                               attributes to SUPPLIER.
#   shipment_confidence(po_id)  fast, per-consignment. Falls on ANY verified
#                               discrepancy, whoever turns out to be
#                               responsible.
#
# "We cannot confirm these units arrived" and "this supplier is unreliable"
# are different claims, and a dispatch claim standing against
# label_created_no_pickup only supports the first. The consequence Track B
# applies follows the attribution, not the inconvenience.
try:                                    # Track A: trust.py at the repo root
    from trust import effective_reliability as _effective_reliability
    from trust import record_incident as _record_incident
    from trust import shipment_confidence as _shipment_confidence
    from trust import trust_write as _trust_write
    from trust import units_confirmed as _units_confirmed
    TRUST_AVAILABLE = True

    _ensured = False

    def _ensure_store() -> None:
        """Track A's trust ledger is a table in the sandbox database, so it
        needs the schema to exist. A StubSandbox run never touches that
        database — the stub is in-memory — so the first trust call would hit a
        missing table. init_db() creates the schema and seeds only when empty,
        which makes it safe to call from here."""
        global _ensured
        if _ensured:
            return
        _ensured = True
        try:
            from sandbox import db
            db.init_db()
        except Exception:               # noqa: BLE001 - reported below, not fatal
            pass

    def _degrade(exc: Exception) -> None:
        global TRUST_DEGRADED
        TRUST_DEGRADED = True

    def reliability_of(supplier_id: str, catalog_score: float) -> float:
        _ensure_store()
        try:
            return _effective_reliability(supplier_id, catalog_score)
        except Exception as exc:        # noqa: BLE001
            _degrade(exc)
            return _fallback_reliability(supplier_id, catalog_score)

    def trust_write(supplier_id: str, event: str) -> None:
        _ensure_store()
        try:
            _trust_write(supplier_id, event)
        except Exception as exc:        # noqa: BLE001
            _degrade(exc)
            _fallback_write(supplier_id, event)

    def shipment_confidence(po_id: str) -> float:
        _ensure_store()
        try:
            return _shipment_confidence(po_id)
        except Exception as exc:        # noqa: BLE001
            _degrade(exc)
            return _fallback_confidence(po_id)

    def units_confirmed(po_id: str) -> bool:
        _ensure_store()
        try:
            return _units_confirmed(po_id)
        except Exception as exc:        # noqa: BLE001
            _degrade(exc)
            return _fallback_confidence(po_id) >= UNCONFIRMED_BELOW

    def record_incident(po_id: str, supplier_id: str, observed: str,
                        expected: str, attribution: str,
                        attribution_basis: str) -> str | None:
        """Always logged. Reputation moves only inside trust.py, and only when
        the attribution is SUPPLIER - Track B does not decide that."""
        _ensure_store()
        try:
            return _record_incident(po_id, supplier_id, observed, expected,
                                    attribution, attribution_basis)
        except Exception as exc:        # noqa: BLE001
            _degrade(exc)
            return _fallback_incident(po_id, supplier_id, attribution)

except ImportError:                     # pragma: no cover - pre-merge path
    TRUST_AVAILABLE = False
    reliability_of = None               # bound below
    trust_write = None
    shipment_confidence = None
    units_confirmed = None
    record_incident = None

# Track B calls reliability_of() and trust_write() and nothing else. Track A's
# trust_read() returns a SupplierTrust record and their effective_reliability()
# returns the float; the pre-merge fallback has only the float. Naming the
# Track-B-facing helper separately keeps that difference at this boundary
# instead of at every call site — and it is why the merge needed one edit here
# rather than one in solver_input.py and another in nodes/investigate.py.


_PENALTY = {"contradicted_claim": 0.14, "late": 0.05,
            "moq_failure": 0.05, "quality_miss": 0.05}
_DELTAS: dict[str, float] = {}


def _fallback_reliability(supplier_id: str, catalog_score: float) -> float:
    """Catalog score less the penalties observed this run, floored at 0.05."""
    return round(max(0.05, catalog_score - _DELTAS.get(supplier_id, 0.0)), 4)


def _fallback_write(supplier_id: str, event: str) -> None:
    _DELTAS[supplier_id] = _DELTAS.get(supplier_id, 0.0) + _PENALTY.get(event, 0.0)


# The shipment axis, in-process. Mirrors trust.py's constants so a degraded
# run reports the same numbers rather than a different scale.
CONFIDENCE_FULL = 1.0
DISCREPANCY_PENALTY = 0.6
UNCONFIRMED_BELOW = 0.5

_INCIDENTS: dict[str, list[dict]] = {}


def _fallback_confidence(po_id: str) -> float:
    return max(0.0, CONFIDENCE_FULL
               - DISCREPANCY_PENALTY * len(_INCIDENTS.get(po_id, [])))


def _fallback_incident(po_id: str, supplier_id: str, attribution: str) -> str:
    log = _INCIDENTS.setdefault(po_id, [])
    log.append({"supplier_id": supplier_id, "attribution": attribution})
    if attribution == "SUPPLIER":        # the only path to reputation
        _fallback_write(supplier_id, "contradicted_claim")
    return f"INC-{sum(len(v) for v in _INCIDENTS.values()):04d}"


def _reset_trust() -> None:
    _DELTAS.clear()
    _INCIDENTS.clear()


if not TRUST_AVAILABLE:                 # pre-merge: the fallback IS the ledger
    reliability_of = _fallback_reliability
    trust_write = _fallback_write
    shipment_confidence = _fallback_confidence
    units_confirmed = lambda po_id: _fallback_confidence(po_id) >= UNCONFIRMED_BELOW  # noqa: E731
    record_incident = (lambda po_id, supplier_id, observed, expected,
                       attribution, attribution_basis:
                       _fallback_incident(po_id, supplier_id, attribution))


# ---- attribution -------------------------------------------------------
#
# sandbox/attribution.py owns this because sandbox/ owns tracking ground
# truth, and attribution is a reading of evidence rather than a judgement
# about a supplier. Track B calls it and never second-guesses the answer.
#
# Their classify() takes a sqlite row; Track B holds a TrackingRecord. The
# adapter below is the whole difference, and it keeps that shape mismatch at
# this boundary instead of in node 3.

try:
    from sandbox.attribution import classify as _classify
    from sandbox.attribution import has_discrepancy as _has_discrepancy
    ATTRIBUTION_AVAILABLE = True
except ImportError:                     # pragma: no cover - pre-merge path
    _classify = None
    _has_discrepancy = None
    ATTRIBUTION_AVAILABLE = False

IN_CARRIER_HANDS = ("picked_up", "in_transit", "at_facility", "out_for_delivery")


def _as_row(tracking) -> dict:
    """A TrackingRecord as the mapping sandbox/attribution.py expects.

    packed_at and tendered_at are absent from the frozen TrackingRecord, so
    they read as None. classify() treats that correctly: with no pack record
    and a label but no pickup, the honest answer is UNATTRIBUTED, which is
    exactly the reading the evidence supports.
    """
    return {
        "po_id": tracking.po_id,
        "supplier_claim": tracking.supplier_claim,
        "tracking_status": tracking.tracking_status,
        "last_movement": tracking.last_movement,
    }


def has_discrepancy(tracking) -> bool:
    if ATTRIBUTION_AVAILABLE:
        return bool(_has_discrepancy(_as_row(tracking)))
    if tracking.supplier_claim != "dispatched":
        return False
    return tracking.tracking_status not in IN_CARRIER_HANDS + ("delivered",)


def attribute(tracking, now) -> tuple[str, str, str, str]:
    """(attribution, basis, observed, expected) for one tracking record."""
    if ATTRIBUTION_AVAILABLE:
        return _classify(_as_row(tracking), now)
    observed = f"tracking_status {tracking.tracking_status!r}"
    if tracking.last_movement is None:
        observed += ", no movement recorded"
    return ("UNATTRIBUTED",
            "no attribution module available; the evidence has not been read",
            observed, "goods with the carrier and moving")


def status() -> dict[str, bool]:
    """Reported in the run banner and in the run_complete audit event, so a
    reader can always tell whether a run used the real solver."""
    out = {"solver": SOLVER_AVAILABLE, "guardrails": GUARDRAILS_AVAILABLE,
           "trust": TRUST_AVAILABLE}
    if TRUST_DEGRADED:
        # Never silent: the trust ledger is the Supplier Risk line, and a run
        # that scored it from an in-process approximation has to say so.
        out["trust_degraded"] = True
    return out
