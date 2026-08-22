"""Track A's solver.

Track B imports `solve` from here and never changes that import. At the
hour-12 merge this one line swaps to the CP-SAT model:

    from .fallback import solve      ->      from .model import solve
"""

from .fallback import solve

__all__ = ["solve"]
