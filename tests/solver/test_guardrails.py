"""Track A §5 item 8 — validate() forces escalation above the approval limit.

This is the only one of the eight checks that does not exercise the solver.
It exercises guardrails/validator.py, which is not built yet (§8.8 puts it at
hour 9), so the module import below skips the file rather than failing it.
The check goes live the moment validator.py lands — nothing here needs
editing then.

Writing it now also pins down the half of validate()'s interface that §3.1
leaves open: the shape of `context`. The track document freezes the
signature, validate(plan: SolverOutput, context: dict) -> Verdict, and the
return type, but not the keys. This file asserts G2 only, so it needs just
`approval_limit`; validator.py must read that key by that name.
"""

import pytest

from contracts.constants import APPROVAL_THRESHOLD
from contracts.models import Allocation, SolverOutput

validator = pytest.importorskip(
    "guardrails.validator",
    reason="guardrails/validator.py is hour-9 work and does not exist yet",
)


def plan(total_cost: float) -> SolverOutput:
    return SolverOutput(
        status="FEASIBLE",
        allocations=[Allocation(supplier_id="SUP-42", units=1000,
                                cost=total_cost, arrival_day=4)],
        total_cost=total_cost,
        requires_approval=total_cost > APPROVAL_THRESHOLD,
        relaxation_used="none",
    )


CONTEXT = {"approval_limit": float(APPROVAL_THRESHOLD),
           "remaining_budget": 400000.0}


@pytest.mark.parametrize("cost", [150000.01, 150001.0, 200000.0, 399999.0])
def test_g2_forces_escalation_above_the_threshold(cost):
    """Any plan over 150,000 is blocked, whatever else is true of it."""
    verdict = validator.validate(plan(cost), CONTEXT)
    assert verdict.forced_escalation is True
    assert "G2" in verdict.fired
    assert verdict.reasons, "a forced escalation must carry a readable reason"


@pytest.mark.parametrize("cost", [0.0, 1000.0, 149999.0, 150000.0])
def test_g2_stays_quiet_at_or_below_the_threshold(cost):
    """The threshold is strictly greater-than, per PS §5.8.

    approval_required = estimated_cost > 150000. A plan costing exactly
    150,000 executes autonomously.
    """
    verdict = validator.validate(plan(cost), CONTEXT)
    assert "G2" not in verdict.fired
