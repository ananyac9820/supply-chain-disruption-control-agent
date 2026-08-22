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

try:                                    # Track A: trust.py at the repo root
    from trust import effective_reliability as _effective_reliability
    from trust import trust_write as _trust_write
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

except ImportError:                     # pragma: no cover - pre-merge path
    TRUST_AVAILABLE = False
    reliability_of = None               # bound below
    trust_write = None

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


def _reset_trust() -> None:
    _DELTAS.clear()


if not TRUST_AVAILABLE:                 # pre-merge: the fallback IS the ledger
    reliability_of = _fallback_reliability
    trust_write = _fallback_write


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
