"""validate(plan, context) -> Verdict — the deterministic gate on every plan.

The LLM does not get to overrule this. Node 4 re-solves on a veto, at most
twice (§7 F-8), and then escalates.

The `context` contract
----------------------
`contracts/models.py` freezes the signature and the return type but not the
keys. These are them. Every key except `approval_limit` is optional: a rule
whose inputs are absent does not fire, so a caller that has not yet computed
projected stock gets no G5 rather than a crash.

| key                                | type            | read by |
|------------------------------------|-----------------|---------|
| `approval_limit`                   | float           | G2 — **frozen name** |
| `remaining_budget`                  | float           | G1 |
| `projected_stock`                   | int             | G5 |
| `safety_stock`                      | int             | G5 |
| `affected_priorities`               | list[str]       | G5 |
| `safety_stock_breach_justification` | str \\| None     | G5 |
| `quotes`                            | list[dict]      | G8 |
| `claims`                            | list[dict]      | G9 |
| `now`                               | datetime \\| str | G8 |

`quotes` entries carry `supplier_id`, `issued_at`, `quote_valid_hours`.
`claims` entries carry `supplier_id` and `status`.
"""

from contracts.models import SolverOutput, Verdict
from guardrails.rules import POST_CHECKS, PRE_SOLVE_RULES


def validate(plan: SolverOutput, context: dict) -> Verdict:
    """Run the six post-checks and fold them into one Verdict.

    `passed` answers "may this plan execute as it stands". A veto means
    re-solve; `forced_escalation` means a human decides. They are independent:
    G2 escalates without vetoing, because re-solving an over-threshold plan
    returns the same plan.
    """
    findings = [f for f in (check(plan, context) for check in POST_CHECKS)
                if f is not None]

    return Verdict(
        passed=not any(f.veto for f in findings) and not any(
            f.forced_escalation for f in findings),
        fired=[f.rule for f in findings],
        reasons=[f.reason for f in findings],
        forced_escalation=any(f.forced_escalation for f in findings),
    )


def vetoed(verdict: Verdict) -> bool:
    """True when node 4 should re-solve rather than escalate.

    A verdict can fail without being a veto — G2 and G12 both fail a plan for
    execution while re-solving is useless. This is the function node 4 should
    branch on, not `passed`.
    """
    return bool(set(verdict.fired) - {"G2", "G12"}) and not verdict.passed


def unreachable_pre_solve_rules() -> tuple[str, ...]:
    """G3, G4, G6, G7, G11 — filters and constraints, never post-checks.

    Named here so a test can assert they are absent from any Verdict. If one
    of them ever appears, the solver built a plan it should have been unable
    to construct.
    """
    return PRE_SOLVE_RULES
