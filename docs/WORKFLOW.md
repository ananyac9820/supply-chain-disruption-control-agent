# Agent workflow

Detection through execution, with the replan loop. Rectangles are
deterministic code, rounded nodes involve the model, the diamond is the human
checkpoint.

```mermaid
flowchart TD
    START([disruption arrives]) --> MON

    MON(["① MONITOR / TRIAGE<br/>scan inbox + open POs<br/><i>LLM: type and severity</i>"])
    MON --> IMP

    IMP["② IMPACT + BASELINE<br/>usable_stock ÷ daily_usage<br/>cumulative shortfall in deadline order<br/>cost of doing nothing"]
    IMP --> INV

    INV(["③ INVESTIGATE<br/><i>LLM: which tool next, and why</i><br/>message supplier · check tracking · RFQ<br/>every claim → GROUNDED / CONTRADICTED / UNVERIFIABLE"])
    INV --> BUILD

    BUILD["build SolverInput<br/>G3 certification + G4 quality floor as pre-filters<br/>effective_reliability from the trust ledger<br/>shipment confidence → claim_contradicted"]
    BUILD --> SOLVE

    SOLVE{"④ SOLVE — CP-SAT<br/>x[s] units × r[p] delay days"}
    SOLVE -->|rung 1| R1["procurement only<br/>r[p] fixed at 0"]
    R1 -->|infeasible| R2["rung 2: free r[p]<br/>production reschedule"]
    R2 -->|infeasible| R3["rung 3: partial coverage<br/>under a hard budget cap"]
    R3 -->|infeasible| R4["rung 4: INFEASIBLE<br/>_diagnose names the binding constraint"]

    R1 --> VAL
    R2 --> VAL
    R3 --> VAL
    R4 --> VAL

    VAL["VALIDATOR — G1–G12<br/>G1 budget · G2 approval · G5 safety stock<br/>G8 quote expiry · G9 unconfirmed units · G12 infeasible"]
    VAL -->|"vetoed() — G1, G5, G8, G9"| RESOLVE
    VAL -->|"needs_fresh_quote() — G8"| INV
    VAL -->|passed or escalate-only| GATE

    RESOLVE["re-solve<br/>max 2 correction rounds"]
    RESOLVE --> SOLVE
    RESOLVE -->|"rounds exhausted"| GATE

    GATE{"⑤ GATE — impact × confidence"}
    GATE -->|"low impact + high confidence"| EXEC
    GATE -->|"anything else, or G2 / G5-high / G10 / G12"| PAUSE

    PAUSE[["interrupt()<br/>state checkpointed, run paused"]]
    PAUSE --> HUMAN
    HUMAN{{"coordinator reads the brief<br/>approve · edit · reject"}}
    HUMAN -->|"Command(resume=…)"| EXEC

    EXEC["⑥ EXECUTE + BRIEF<br/>ERP writes · decision brief · audit flush<br/>register the assumptions this plan depends on"]
    EXEC --> WATCH

    WATCH["ASSUMPTION WATCHER<br/>re-checks recorded stock, quality,<br/>demand, priority, quote validity"]
    WATCH -->|"assumption broken"| INV
    WATCH -->|holds| DONE([run complete])

    classDef det fill:#e8eef7,stroke:#41618f,color:#12243d
    classDef llm fill:#f3ecf8,stroke:#7d5ba6,color:#2c1a3d
    classDef human fill:#fdf3e3,stroke:#c08a2e,color:#3d2c12
    class IMP,BUILD,R1,R2,R3,R4,VAL,RESOLVE,EXEC,WATCH det
    class MON,INV llm
    class PAUSE,HUMAN human
```

## The loops, and why each exists

**Validator → re-solve.** A guardrail veto sends the plan back to the solver,
at most twice, then escalates. The model does not get to overrule the
validator; a constraint you ask a model to respect is a suggestion.

**G8 → node ③, not the solver.** An expired quote is stale information, not a
bad plan. Re-solving the same inputs drops that supplier for the rest of the
run when the remedy is one RFQ away, so `needs_fresh_quote()` routes it back
to investigation for a fresh quote first. This is the one veto whose fix is
not a re-solve.

**Assumption broken → node ③.** Every plan records the facts it depends on —
a quote's validity window, a stock figure, a priority. When the environment
changes one, the watcher invalidates it and the graph re-enters investigation
carrying `broken_assumption`. Replanning is then deterministic and visible,
rather than something the model might or might not notice.

**Escalation resumes, it does not restart.** `interrupt()` checkpoints the
full state. `Command(resume=…)` picks up from the pause with everything the
run had already learned — the tool results, the verifications, the trust
updates — rather than starting again.
