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
    GUARDRAILS_AVAILABLE = True
except ImportError:                     # pragma: no cover - pre-merge path
    validate = None                     # type: ignore[assignment]
    GUARDRAILS_AVAILABLE = False

try:                                    # Track A: trust.py at the repo root
    from trust import trust_read, trust_write
    TRUST_AVAILABLE = True
except ImportError:                     # pragma: no cover - pre-merge path
    TRUST_AVAILABLE = False

    _PENALTY = {"contradicted_claim": 0.14, "late_delivery": 0.05,
                "moq_failure": 0.05, "quality_shortfall": 0.05}
    _DELTAS: dict[str, float] = {}

    def trust_read(supplier_id: str, catalog_reliability: float) -> float:
        """Catalog score less the penalties observed this run, floored at 0.05."""
        return round(max(0.05, catalog_reliability - _DELTAS.get(supplier_id, 0.0)), 4)

    def trust_write(supplier_id: str, event: str) -> float:
        _DELTAS[supplier_id] = _DELTAS.get(supplier_id, 0.0) + _PENALTY.get(event, 0.0)
        return _DELTAS[supplier_id]

    def _reset_trust() -> None:
        _DELTAS.clear()


def status() -> dict[str, bool]:
    """Reported in the run banner and in the run_complete audit event, so a
    reader can always tell whether a run used the real solver."""
    return {"solver": SOLVER_AVAILABLE, "guardrails": GUARDRAILS_AVAILABLE,
            "trust": TRUST_AVAILABLE}
