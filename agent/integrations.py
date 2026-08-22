"""Import guards for the two Track A surfaces node 4 calls.

Track B calls solve() and validate(); it never implements them. Until Track A
merges those folders into main, the imports below fail and the flags go False,
which lets the hollow graph walk end to end without Track B inventing a solver.

At hour 12 both imports resolve and nothing else in agent/ changes.
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


def status() -> dict[str, bool]:
    """Reported in the run banner and in the run_complete audit event, so a
    reader can always tell whether a run used the real solver."""
    return {"solver": SOLVER_AVAILABLE, "guardrails": GUARDRAILS_AVAILABLE}
