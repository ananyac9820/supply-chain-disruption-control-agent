# sandbox/ — the simulated ERP

FastAPI over one SQLite file. Localhost only. No auth, no async workers, no
message queue (§4.6, §8.1).

## Status: complete

- `db.py` — twelve tables, the seed loader, the simulated clock
- `seed/` — the hand-authored catalog
- `app.py` — the sixteen routes of §4.6
- `client.py` — `HttpSandbox`, loopback only, standard library transport
- `supplier_sim.py` — honest / vague / contradictory personas
- `chaos.py` — `POST /sim/inject` firing H-01 … H-10, plus `/sim/inject/sequence`

Outstanding: PS §16 filler seed rows (see below). Every scenario-carrying row
exists.

## The simulated clock

`GET /sim/clock` reads it; two things move it. Sending a supplier a message
advances it one hour, which is what makes "the reply arrives on the next
tick" mean something without a scheduler running: the reply is written with
`visible_at` at the new time, so it cannot be read inside the call that
provoked it. `/sim/inject/sequence` advances it by each step's
`delay_minutes`, so a timed cascade needs nobody typing during the pitch.

Quote expiry (G8) is measured against this clock, so a plan built on a quote
issued six messages ago is genuinely stale rather than notionally stale.

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

Twelve tables. Additions beyond PS §5, each marked ADD in `db.py`:

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
- `sim_flags` — chaos side-effects that apply to records not yet created.
  H-07 withdraws expedite from *future* quotes, which is not a column edit.

## H-03 targets a different supplier than §4.8 names

§4.8 says H-03 should "drop SUP-18's quality_score below min_quality, or
strip a certification". In the seeded catalog SUP-18 is already uncertified
*and* already under the floor — that is its whole purpose. Mutating it
changes nothing the agent can observe, so H-03 would report a disruption and
inject none.

H-03 therefore targets the cheapest supplier that currently passes both
filters, and drops it below the floor. `params.supplier_id` overrides the
choice. The test asserts the target was eligible before and is not after,
which is the property the event is for.

`SIM_EPOCH` is 2026-09-02T10:00, matching `contracts/stub_sandbox.NOW`, so
seeded deadlines land on the same day numbers whenever the sandbox starts.

## Safety

The §2.6 proof grep must return **nothing** over `sandbox/ solver/
guardrails/`. The command itself is in `SAFETY.md` at the repo root, and it
stays there rather than here: a copy inside a grepped directory matches its
own pattern and turns a clean proof into a hit a judge has to be talked
through.

`sandbox/client.py` is the one file that speaks HTTP, over loopback only,
using the standard library. `SAFETY.md` lists it explicitly.
