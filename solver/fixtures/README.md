# Solver fixtures f01–f07

Hand-computed test cases for the CP-SAT model, per Track A §5 items 1–7.

**These expectations were derived by hand from the §4.1 objective. No solver
produced them.** That is the point: the objective has four weighted terms and
it is genuinely easy to build something that runs, returns a plausible
answer, and is wrong. A fixture whose expected output came out of the code it
is testing catches nothing.

Each case is a pair: `fNN_name.json` is a `SolverInput`, `fNN_name.expected.json`
is the `SolverOutput` the model must produce. All sixteen files validate
against `contracts/models.py`.

## Reading the working

The §4.1 objective, minimised:

    1.0   * sum(x[s] * unit_price[s])                  procurement cost
  + 8000  * sum(priority_weight[p] * r[p])             lateness
  + 40    * sum(x[s] * (1 - effective_reliability[s])) supplier risk
  + 500   * sum(y[s])                                  anti-fragmentation

Most cases are settled by an **adjusted unit cost** — `unit_price + 40 × (1 −
effective_reliability)` — which folds the risk term into a per-unit number.
The 500-per-supplier term then only matters when a split is otherwise a tie.

Two conventions for whoever writes the test runner:

- Allocations are listed sorted by `supplier_id`. Compare as a set, or sort
  both sides; order is not part of the contract.
- `status` is `OPTIMAL` wherever CP-SAT can prove optimality. **The greedy
  `fallback.py` never claims OPTIMAL** — it reports `FEASIBLE` for any plan
  that buys something, because a greedy sweep has no proof. When running
  these against the fallback, compare allocations, cost, reschedules and
  `relaxation_used`, and treat `OPTIMAL`/`FEASIBLE` as equivalent.

---

## f01_simple — gap 400, three suppliers

On hand 550 − 150 safety = 400. One order needs 800 by day 5, so the gap is
400. All three suppliers are certified, arrive in time, and can cover it
alone, so this is a straight three-way comparison:

| supplier | price | eff.rel | 400 units | + risk | + split | total |
|---|---|---|---|---|---|---|
| SUP-A | 100 | 0.90 | 40,000 | 1,600 | 500 | **42,100** |
| SUP-B | 110 | 0.95 | 44,000 | 800 | 500 | 45,300 |
| SUP-C | 130 | 0.99 | 52,000 | 160 | 500 | 52,660 |

SUP-A wins on adjusted cost (104/unit vs 112 and 130.4), and no split can beat
a single supplier that is cheapest on both terms. **SUP-A × 400, 40,000.**

## f02_split — no single supplier has enough

Gap 800; the largest supplier holds 600, so a split is forced. Adjusted unit
costs are A 104, B 114, C 129 (all three share eff.rel 0.90). Fill cheapest
first: A to its 500 ceiling, the remaining 300 from B, which clears B's MOQ
of 200.

    A 500 × 100 = 50,000        risk 40 × 800 × 0.10 = 3,200
    B 300 × 110 = 33,000        split 2 × 500        = 1,000
    cost         83,000         objective            = 87,200

Alternatives: A+C is 91,700; B+C is 98,200; overbuying A 500 + B 400 raises
cost to 94,000 for units nobody needs. **A × 500 + B × 300, 83,000.**

## f03_moq_overbuy — the cheapest option forces a 300-unit surplus

Gap 200. SUP-A is at 100/unit but will not sell fewer than 500. SUP-B will
sell exactly 200, at 270.

    SUP-A, 500 units (300 surplus):  50,000 + 2,000 risk + 500 = 52,500
    SUP-B, 200 units:                54,000 +   800 risk + 500 = 55,300

The overbuy wins by 2,800, so it is chosen — and it is chosen *because the
total still wins*, not because surplus is free. Flip SUP-B to 240/unit and the
answer flips with it. **SUP-A × 500, 50,000.**

## f04_uncertified — SUP-18 is cheapest AND fastest

The real COMP-104 catalog. On hand 390 − 150 = 240, PROD-882 needs 700 by
day 4, so the gap is 460.

SUP-18 at 104/unit and 3 days beats every other row on both price and speed.
It is removed twice over: C3 (no Automotive-Grade certification) and C4
(quality 0.79 < min_quality 0.90). It must not appear in the output at all —
not as an allocation, not as a rejected-but-cheaper note.

Of what remains, only SUP-42 arrives by day 4 (SUP-55 is 5 days, SUP-21 and
SUP-37 are 6). Its MOQ of 300 is under the gap and its 700 units cover it.
**SUP-42 × 460, 60,720.**

## f05_reschedule — procurement alone cannot do it

The corrected seed: PROD-914 (700 units, day 2, low, delayable 5) falls due
*before* PROD-882 (700 units, day 4, high, not delayable). On hand 240.

Rung 1, `r[p] = 0`: PROD-914 needs 460 units by day 2. The fastest eligible
supplier is SUP-42 at 4 days — SUP-18's 3 days is disqualified. No allocation
can satisfy it, so procurement-only is **INFEASIBLE on the deadline**.

Rung 2 frees `r[PROD-914]`. Capacity reachable by day 2 + d:

| d | day | suppliers with lead ≤ day | units reachable | ≥ 1,160 needed? |
|---|---|---|---|---|
| 3 | 5 | SUP-42, SUP-55 | 1,050 | no |
| 4 | 6 | all four | 2,450 | **yes** |

So d = 4 is the smallest delay that admits enough supply, and d = 5 buys
nothing extra while costing another 8,000 in lateness. With d = 4, PROD-882
(day 4) is now served first and only SUP-42 arrives by then, forcing
x[SUP-42] ≥ 460. The remaining 700 goes to the cheapest adjusted cost:
SUP-21 at 129.2 (vs SUP-42 139.6, SUP-55 144.0, SUP-37 145.8).

    SUP-42 460 × 132 = 60,720     risk  40 × (460×0.19 + 700×0.28) = 11,336
    SUP-21 700 × 118 = 82,600     split 2 × 500                    =  1,000
    cost            143,320       lateness 8000 × 1.0 × 4          = 32,000
                                  objective                        = 187,656

143,320 is under the 150,000 threshold, so `requires_approval` is false.
**SUP-21 × 700 + SUP-42 × 460, PROD-914 delayed 4 days, `relaxation_used =
"reschedule"`.**

> ⚠️ **This fixture depends on a reading of C6 — flagging rather than
> guessing.** As printed in §4.1, C6 is written per order:
>
>     usable_stock - safety_stock + sum(arrivals by deadline[p] + r[p]) >= units_required[p]
>
> Taken literally, every order compares against the *same* `usable_stock`, so
> the 240 units on hand are counted once for PROD-914 and again for PROD-882.
> Under that reading the answer is 460 units total and both orders are
> declared satisfiable from stock that only exists once — physically wrong,
> and it makes §7 B-4 ("two production orders compete for the same
> component") unrepresentable.
>
> These expectations use the **cumulative** reading: orders are walked in
> deadline order and each consumes stock the earlier ones left. That is the
> only reading under which competition is real. It changes f05's answer from
> 460 units to 1,160, so it needs to be a deliberate decision, not a silent
> one — and the CP-SAT model in `solver/model.py` must implement the same
> reading. No other fixture here has two orders, so nothing else moves.

## f06_infeasible — 500 short, 200 in the world

Gap 500, one supplier holding 200 units, nothing delayable, partial coverage
off. Certification, quality, MOQ, budget and lead time are all satisfiable —
there simply are not enough units. **INFEASIBLE, `binding_constraint =
"available_quantity"`.**

The binding constraint is the whole value of this case: Person B puts that
string into the escalation brief, so "available_quantity" and "deadline" are
not interchangeable answers.

## f07_trust_before / f07_trust_after — the ledger must change the answer

Identical inputs but for one field. Gap 400; SUP-X is cheaper, SUP-Y is more
reliable; each can cover the gap alone.

    before   SUP-X eff.rel 0.90   40,000 + 1,600 + 500 = 42,100   <- wins
             SUP-Y eff.rel 0.95   42,000 +   800 + 500 = 43,300

Between the runs, SUP-X is caught in one contradicted claim. Per §4.5 that is
a 0.15 penalty: 0.90 → 0.75.

    after    SUP-X eff.rel 0.75   40,000 + 4,000 + 500 = 44,500
             SUP-Y eff.rel 0.95   42,000 +   800 + 500 = 43,300   <- wins

A 50/50 split is 43,200 before and 44,400 after, so it never wins either run.
**SUP-X × 400 before, SUP-Y × 400 after.**

Note what is *not* set here: `claim_contradicted` stays false in both files.
That flag is C8, which zeroes a supplier outright. This pair isolates the
trust ledger's effect through the risk term alone — the point of §4.5 is that
`effective_reliability` changes the allocation on its own, inside a single
run. A ledger that only matters when it also trips a hard filter is
decoration.

---

## Known divergence: f07 under the greedy fallback

Run against `solver/fallback.py`, seven of the eight cases match these
expectations exactly (allowing FEASIBLE for OPTIMAL). **f07_trust_after does
not**: the fallback returns SUP-X × 400 where the objective says SUP-Y × 400.

This is not a fixture error, and it is not a bug in the fallback either.
§4.3 specifies the greedy as "sort certified suppliers by unit price" — price
only. The risk term is the entire mechanism by which a trust decrement
changes an allocation, so a price-sorted greedy cannot express D-4 at all.
SUP-X stays cheapest no matter how far its reliability falls.

It matters because the fallback is the insurance policy: the cut list ends
with "CP-SAT itself, shipping fallback.py instead". If that cut is ever
taken, the supplier-risk story (15% of the rubric, demo Act 2) stops working
silently — the trust ledger still updates, still prints, and no longer
changes a single decision.

The fix is one line — sort by `unit_price + 40 × (1 − effective_reliability)`
instead of `unit_price` — and it changes no other fixture here: the adjusted
order matches the price order in f01–f04, and in f05 the day-4 deadline
forces SUP-42 first either way. It is left undone deliberately, because §4.3
says price and the track document wins. **Person A's call.**
