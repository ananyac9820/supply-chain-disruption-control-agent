# Supply Chain Disruption Control Agent

HOP 2026 · AI / Agentic Systems · Layer 3. An autonomous procurement
disruption controller over a fully simulated ERP sandbox. When a supplier
delay, inventory correction, quality failure or demand spike threatens a
production order, the agent investigates through tools, verifies supplier
claims against tracking ground truth, computes a recovery plan that jointly
allocates emergency purchase quantities and production reschedules, blocks
itself at the approval threshold, writes back to the simulated ERP, and
emits a decision trail an operations manager can read.

**The model judges and explains. The code computes and guarantees.**

## Status

Hour 0. Repo skeleton and `contracts/` only. Nothing else has started —
that is deliberate (§4.3 of the master plan): the frozen contract is what
buys both tracks ten hours of independence.

## Layout

    contracts/   frozen interface, joint, hours 0-1.5   <- the only shared surface
    sandbox/     Person A — 15 REST endpoints, SQLite, seed data, chaos injector
    solver/      Person A — CP-SAT procurement + reschedule model
    guardrails/  Person A — G1-G12 validator
    agent/       Person B — LangGraph graph and nodes
    output/      Person B — audit writer, CLI, brief, dashboard
    tests/       contract/ (both sandboxes) · solver/ (fixtures) · scenarios/ (end to end)

Ownership is enforced, not advisory. See `CLAUDE.md`.

`contracts/` is five files: `models.py`, `state.py`, `audit_schema.py`,
`stub_sandbox.py`, `constants.py`. The master plan also lists an
`openapi.json`; it is deliberately **not** part of the freeze — Track B
consumes the Python `SandboxClient` Protocol, not a generated spec. Do not
add it back.

## Next steps

1. Person B pastes their `CLAUDE.md` section from the Track B document.
2. Both people review `contracts/` together, then:

       git add -A && git commit -m "[joint] repo skeleton, contracts, CLAUDE.md, SAFETY.md"
       git tag contracts-v1
       git checkout -b track-a contracts-v1   # Person A
       git checkout -b track-b contracts-v1   # Person B

3. After the tag, `contracts/` is frozen. No change without both track
   leads agreeing in the channel and both re-running `pytest tests/contract/`.

## Safety

Fully simulated, per problem statement §18. See `SAFETY.md`.
