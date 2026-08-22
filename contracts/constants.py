"""Shared constants — Track A §3.5.

Both tracks read these. Values marked (assumed) are master plan §11
assumptions to confirm with the organisers; each is one constant here, so
correcting one is a one-line change, not a refactor.

FROZEN AT HOUR 1.5.
"""

APPROVAL_THRESHOLD = 150_000        # G2, /approval/check — PS §5.2 / §5.8
EMERGENCY_BUDGET = 400_000          # G1 — assumed, confirm with organisers
TOOL_BUDGET_PER_DISRUPTION = 15     # G10 — assumed if organisers give no cap
CACHE_TTL_SIM_SECONDS = 30          # tool-call read cache (D-3)

# Solver objective weights (§4.1 / §8.5). W_LATE is a business judgement:
# a priority-weighted production day is worth roughly 8,000, which is what
# makes continuity dominate cost in line with the 35/20 rubric split.
W_LATE = 8_000
W_RISK = 40
W_SPLIT = 500

PRIORITY_WEIGHT = {"high": 5.0, "medium": 2.0, "low": 1.0}
MAX_DELAY_DAYS = {"high": 0, "medium": 2, "low": 5}
