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
| `shipment_confidence`               | dict[str,float] | G9 |
| `unconfirmed_below`                 | float           | G9 |
| `claims`                            | list[dict]      | G9 (legacy) |
| `now`                               | datetime \\| str | G8 |

`quotes` entries carry `supplier_id`, `issued_at`, `quote_valid_hours`.
`shipment_confidence` maps supplier_id to a confidence in 0..1; below
`unconfirmed_below` (default 0.5) that supplier's units are not counted.
`claims` is still read for callers that have not moved over: a status of
CONTRADICTED is treated as confidence 0.0.
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


# G8 is the one veto whose fix is not a re-solve. An expired quote is stale
# information, not a bad plan: re-solving the same inputs drops that supplier
# for the rest of the run (C7 forces y[s] = 0), when the actual remedy is one
# RFQ away. Track B routes G8 back to node 3 for a fresh quote before
# re-solving, which is correct and is a refinement of vetoed(), not a
# disagreement with it.
REQUOTE_RULES = frozenset({"G8"})

# Rules that fail a plan for execution while re-solving cannot help: an
# over-threshold plan re-solves to itself, and G12 fires only once the
# relaxation ladder has already run out.
ESCALATE_ONLY_RULES = frozenset({"G2", "G12"})


def vetoed(verdict: Verdict) -> bool:
    """True when the plan is recoverable in the loop rather than by a human.

    Branch on this, not on `passed`. A verdict can fail without being a veto:
    G2 and G12 both fail a plan for execution while re-solving is useless.

    This answers *whether* to recover, not *how*. The recoveries differ:

      G1  re-solve under a tighter cap — same inputs, lower budget
      G5  re-solve preserving safety stock, or record a justification
      G9  re-solve without those units; they cannot be confirmed
      G8  **fetch a fresh quote first** — see REQUOTE_RULES and
          needs_fresh_quote(). Re-solving directly is permitted and will
          terminate, but it discards a supplier that one RFQ would restore.

    A reader who takes "veto means re-solve" literally will implement the
    lossy path for G8. Check needs_fresh_quote() before re-solving.
    """
    return bool(set(verdict.fired) - ESCALATE_ONLY_RULES) and not verdict.passed


def needs_fresh_quote(verdict: Verdict) -> bool:
    """True when re-investigation should precede the re-solve.

    Only G8 today. Node 3 re-RFQs the named supplier, then node 4 re-solves
    with a quote that is no longer stale.
    """
    return bool(set(verdict.fired) & REQUOTE_RULES)


def unreachable_pre_solve_rules() -> tuple[str, ...]:
    """G3, G4, G6, G7, G11 — filters and constraints, never post-checks.

    Named here so a test can assert they are absent from any Verdict. If one
    of them ever appears, the solver built a plan it should have been unable
    to construct.
    """
    return PRE_SOLVE_RULES
