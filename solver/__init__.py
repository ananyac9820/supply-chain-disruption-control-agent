"""Track A's solver.

Track B imports `solve` from here and never changes that import. This is the
line the hour-12 merge flips:

    from .fallback import solve      ->      from .model import solve

Flipped. `fallback.solve` remains importable and tested — it is the insurance
policy if CP-SAT misbehaves late, and reverting is this one line.
"""

from .model import solve

__all__ = ["solve"]
