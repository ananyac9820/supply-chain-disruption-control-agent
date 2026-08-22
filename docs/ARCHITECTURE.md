# Architecture

An autonomous procurement disruption controller over a fully simulated ERP
sandbox. When a supplier delay, an inventory correction, a quality failure or
a demand spike threatens a production order, the agent investigates through
tools, verifies supplier claims against tracking ground truth, computes a
recovery plan that jointly allocates emergency purchases *and* production
reschedules, blocks itself at the approval threshold, writes back to the
simulated ERP, and emits a decision trail an operations manager can read.

**The model judges and explains. The code computes and guarantees.** That
boundary is the whole design, and it is visible in the file layout.

## The six nodes

```
① MONITOR / TRIAGE      agent/nodes/monitor.py
   deterministic scan of inbox and open POs, then the LLM classifies
   disruption type and severity

② IMPACT + BASELINE     agent/nodes/impact.py + agent/impact_math.py
   no LLM. usable_stock ÷ daily_usage, cumulative shortfall in deadline
   order, and the cost of doing nothing — the denominator every plan is
   reported against

③ INVESTIGATE           agent/nodes/investigate.py
   the LLM chooses which tool to call next and states why; every supplier
   claim is tagged GROUNDED / CONTRADICTED / UNVERIFIABLE against tracking

④ SOLVE                 agent/nodes/plan.py → solver/model.py
   CP-SAT, never the LLM. x[s] units per supplier, r[p] delay days per
   production order, four-rung relaxation ladder, then guardrails/

⑤ GATE                  agent/nodes/gate.py
   two-axis (impact × confidence). Below threshold and high confidence
   auto-executes; anything else interrupt()s and keeps full state

⑥ EXECUTE + BRIEF       agent/nodes/execute.py + output/
   ERP writes, decision brief, audit flush, and the assumptions this plan
   now depends on
```

## What is deterministic, and why

The rubric puts Production Continuity at 35% and Cost Control at 20%. Both
are arithmetic outcomes. An LLM that computes 390 ÷ 90 wrong on one run in
twenty costs the largest line on the scorecard, and does so invisibly — the
answer looks plausible.

| computed in code | where | why not the model |
|---|---|---|
| coverage days | `agent/impact_math.py` | one wrong division loses the 35% line |
| cumulative shortfall | `agent/impact_math.py` | orders compete for one stock pool; the walk must be exact |
| order split across suppliers | `solver/model.py` | MOQ + availability + budget + deadline is an integer program |
| production reschedule | `solver/model.py` | same model, same solve |
| all twelve guardrails | `guardrails/` | a constraint you ask a model to respect is a suggestion |
| approval threshold | `guardrails/rules.py` G2 | a binary comparison against 150,000 |
| baseline counterfactual | `solver/build.py` | arithmetic |
| reputation and confidence | `trust.py` | arithmetic |

The LLM does exactly four things, and they are the four things language
models are actually good at:

1. classify disruption type and severity
2. decide which tool to call next, and state why
3. judge whether a supplier reply is vague, evasive or inconsistent with
   evidence — a genuine language task
4. write the human-readable brief, **after** the solver has answered

`agent/llm.py` enforces this at the type level: every call returns a typed
object — a classification or a selection — and none of them is a number that
feeds a decision.

### The boundary, in code

`agent/nodes/plan.py` is where it is easiest to see:

```python
solver_input = build_solver_input(...)     # deterministic assembly
out          = solve(solver_input)          # CP-SAT decides the numbers
verdict      = validate(out, context)       # G1–G12 decide if it may execute
rationale    = llm.explain_plan({...})      # the model explains what happened
```

The model is called last and cannot change any of the three lines above it.
On a guardrail veto the graph re-solves — at most twice — and then escalates.
It never asks the model to overrule the validator.

### The degraded path

`agent/llm.py` carries two implementations behind one protocol: `AnthropicLLM`
when a credential resolves, `RuleBasedLLM` when none does. Every run records
which was used, in the `run_complete` audit event and in the CLI banner, so a
rule-based run cannot be mistaken for a live one. A demo that dies when the
API is unreachable is worse than one that degrades visibly.

## The two-track split and the merge contract

Four people, two tracks, one shared file set frozen at hour 1.5.

**Track A — "The World."** No LLM call anywhere. `sandbox/` (sixteen routes,
SQLite, hand-authored catalog, supplier personas, chaos injector), `solver/`
(CP-SAT + greedy fallback), `guardrails/` (G1–G12), `trust.py` (the two-axis
ledger). Every line unit-testable with no API key and nothing running.

**Track B — "The Brain."** `agent/` (the LangGraph state machine, tool ledger,
assumption register, escalation gate) and `output/` (audit JSONL, CLI trace,
decision brief, dashboard).

The two tracks share exactly one thing: `contracts/`.

```
contracts/
├── models.py         Pydantic domain models + the solver and guardrail interfaces
├── state.py          AgentState TypedDict
├── audit_schema.py   AuditEvent and its twelve event types
├── constants.py      the shared numbers
└── stub_sandbox.py   an in-process fake implementing every endpoint
```

`stub_sandbox.py` is what bought the independence. Track B imported it and ran
end to end from hour 2 while `sandbox/` did not yet exist. The real sandbox
had to pass the same `tests/contract/` suite the stub passes, so the merge was
a base-URL swap:

```python
SANDBOX = StubSandbox()                            # before
SANDBOX = HttpSandbox("http://localhost:8000")     # after
```

Same for the solver: Track B coded against a greedy stub returning a valid
`SolverOutput`; Track A swapped in CP-SAT behind the same `from solver import
solve`. If CP-SAT had misbehaved late, reverting was one line, and
`solver/fallback.py` is still tested against every fixture for exactly that
reason.

`tests/contract/` runs one file against both clients — 24 checks, 48 runs — so
any divergence between stub and live sandbox surfaced as a test failure rather
than as an integration surprise.
