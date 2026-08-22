# sandbox/ — the simulated ERP

FastAPI over one SQLite file. Localhost only. No auth, no async workers, no
message queue (§4.6, §8.1).

## Status: schema + seed + one endpoint

Stopped here deliberately, per §8.5: *"Show me the schema and one endpoint
before generating the rest."* Reviewing 800 lines of generated CRUD in one
pass is how contract drift gets in.

Built:
- `db.py` — the eleven tables, the seed loader, the simulated clock
- `seed/` — the hand-authored catalog
- `app.py` — `GET /inventory` and `GET /inventory/{component_id}` (T-01)

Not built yet: the other fourteen endpoints (§4.6), `supplier_sim.py` (§4.8
personas), `chaos.py` (the ten H-events), and the `HttpSandbox` client that
`tests/contract/` will run against alongside `StubSandbox`.

## Seed data — engineered, not sampled

Only the rows that carry a scenario are written. PS §16 asks for 25
components / 14 suppliers / 30 POs / 8 production orders / 16 messages; the
difference is filler for realism and is **not written yet**. §4.7 is explicit
that filler is twenty minutes of work and the five COMP-104 supplier rows are
the actual test suite:

| supplier | price | lead | avail | quality | reliab | MOQ | certs | purpose |
|---|---|---|---|---|---|---|---|---|
| SUP-21 | 118 | 6 | 1000 | 0.91 | 0.72 | 200 | ISO-9001, Automotive-Grade | incumbent; lies about dispatch (H-08) |
| SUP-42 | 132 | 4 | 700 | 0.94 | 0.81 | 300 | ISO-9001, Automotive-Grade | the correct primary answer |
| SUP-37 | 141 | 6 | 400 | 0.96 | 0.88 | 100 | ISO-9001, Automotive-Grade | reliable but short (H-04) |
| SUP-18 | 104 | 3 | 900 | 0.79 | 0.65 | 250 | ISO-9001 | cheapest AND fastest, uncertified (H-03, H-05) |
| SUP-55 | 126 | 5 | 350 | 0.92 | 0.55 | 350 | ISO-9001, Automotive-Grade | MOQ == availability, forces overbuy |

A cheapest-first agent picks SUP-18 and fails. A fastest-first agent also
picks SUP-18. Randomly generated suppliers do not contain this shape, so
H-03 and H-05 would silently never fire and the agent would look right for
the wrong reason.

COMP-104 is 420 current / 390 usable / 90 daily / 150 safety — coverage
390 ÷ 90 = 4.33 days, the number in the PS §17 example.

**PROD-914's deadline is 2026-09-04, not the Sept 8 printed in §4.7.** It has
to fall due before PROD-882 (Sept 6) or delaying it releases nothing for the
high-priority order and rung 2 of the ladder never binds. This matches
`contracts/stub_sandbox.py`, which is the authority — the two must agree or
`tests/contract/` fails by construction.

## Schema notes

Eleven tables. Additions beyond PS §5, each marked ADD in `db.py`:

- `components.required_certifications`, `components.min_quality` — what G3 and
  G4 filter on.
- `production_orders.max_delay_days` — the solver's `r[p]` bound.
- `suppliers.persona` — honest / vague / contradictory, assigned at seed time
  (§4.8).
- `messages.visible_at` — a queued persona reply is invisible to `/inbox`
  until its tick, so a follow-up and its answer cannot arrive together.
- `quotes` — issued quotes are persisted so `quote_valid_hours` can expire
  them (G8) rather than being recomputed as fresh on every read.
- `erp_log` — every `/erp/update` write with its full payload, so the demo can
  prove the writes landed.
- `supplier_trust` — the §4.5 ledger, same file, four counters. `trust.py`
  owns the read/write functions.
- `sim_clock`, `chaos_log` — `GET /sim/clock` and the injector's history.

`SIM_EPOCH` is 2026-09-02T10:00, matching `contracts/stub_sandbox.NOW`, so
seeded deadlines land on the same day numbers whenever the sandbox starts.

## Safety

    grep -rE "smtplib|imaplib|requests\.|httpx\.|stripe|boto3" sandbox/ solver/ guardrails/

Returns nothing. Run it before submission.
