# Sample run, annotated

Produced by `python demo/run_acts.py --reset`. Reproducible: three consecutive
`--reset` runs are byte-identical, because the simulated clock starts from a
fixed epoch and CP-SAT is pinned to a single search worker.

Full untouched output: [`demo/BACKUP_RUN.txt`](../demo/BACKUP_RUN.txt).

---

## Act 1 — the disruption, and the cost of doing nothing

H-01 pushes PO-7712 five days out. The agent reads inventory:

```
current_stock 420 · usable_stock 390 · safety_stock 150 · daily_usage 90
coverage 4.33 days   (usable, not the 4.67 the header implies)
```

**Reason from `usable_stock`, never `current_stock`.** The header figure is
30 units higher, and an agent that trusts it computes 4.67 days of cover
instead of 4.33. That gap is scenario B-1/H-02 and it is the single most
common way to fail this problem.

The shortfall is walked in deadline order, because two orders compete for one
stock pool:

```
on hand above safety stock: 390 - 150 = 240
PROD-914 needs  700 · covered  240 · short  460
PROD-882 needs  700 · covered    0 · short  700
cumulative shortfall 1160 units · 12.89 production days · misses PROD-914, PROD-882
```

Note PROD-914 falls due *first* (Sept 4) despite being the low-priority order.
That ordering is what makes rescheduling a real lever in Act 3 — a delayable
order consuming stock ahead of an immovable one is precisely the situation
where moving it out frees something.

The 1,160-unit shortfall is the denominator. Every plan that follows is
reported as a delta against it, so "expensive" means something measurable
rather than being an adjective.

---

## Act 2 — a vague reply, a claim, and evidence that does not support it

SUP-55 is asked for status and commits to nothing:

```
"We are looking into this and will update you soon."
```

No date, no quantity. The plan cannot rest on that, so the agent challenges it
with a question naming both:

```
"Understood. Confirmed: 300 units, dispatch 2026-09-05, delivery by 2026-09-09."
```

Then SUP-21 claims dispatch on PO-7712, and the agent checks tracking rather
than believing it:

```
supplier_claim 'dispatched' · tracking_status 'label_created_no_pickup' · last_movement None

incident INC-0001  attribution UNATTRIBUTED
    observed: tracking_status 'label_created_no_pickup', no movement recorded
    expected: goods with the carrier and moving
    basis:    a label exists but no pickup was scanned; consistent both with
              goods never tendered and with a collection that was never made

shipment_confidence(PO-7712)  1.00 -> 0.40   these units cannot be counted
reputation (effective_reliability)  0.72 -> 0.72   unchanged
```

**Two different things happen, and keeping them apart is the point.** The
shipment becomes uncountable. The supplier's reputation does not move, because
the evidence does not establish that the supplier is at fault — a printed
label with no pickup scan fits a supplier that never tendered the goods *and*
a courier that never came. See [SUPPLIER_RELIABILITY.md](SUPPLIER_RELIABILITY.md).

---

## Act 3 — what the unverifiable shipment costs

The agent solves the same problem twice: once counting SUP-21's units, once
without them.

**Counting them:**

```
rung 1, procurement only:  INFEASIBLE   binding constraint: deadline
full ladder:               OPTIMAL · reschedule · 143,320.00 · approval not required
    SUP-21    700 units  @day 6     82,600.00
    SUP-42    460 units  @day 4     60,720.00
    delay PROD-914 by 4 days
```

**Not counting them:**

```
rung 1, procurement only:  INFEASIBLE   binding constraint: deadline
full ladder:               OPTIMAL · reschedule · 152,010.00 · approval REQUIRED
    SUP-37    110 units  @day 6     15,510.00
    SUP-42    700 units  @day 4     92,400.00
    SUP-55    350 units  @day 5     44,100.00
    delay PROD-914 by 4 days
```

```
units counted       143,320.00   approval not required
units unconfirmed   152,010.00   approval required
                  ------------
cost of the gap       8,690.00
```

### Why 8,690 is the whole story

Read the two solves as a causal chain, not as two numbers:

1. **Procurement alone cannot solve this, either way.** Both rung 1 solves
   return INFEASIBLE with binding constraint `deadline`. PROD-914 needs 460
   units by day 2 and the fastest certified supplier is four days out. No
   amount of buying fixes a timing problem.

2. **Rescheduling is what makes it solvable.** Freeing `r[PROD-914]` and
   moving it out four days — past PROD-882's deadline — releases the on-hand
   stock the high-priority order needs. Both solves land on exactly four days,
   so **rescheduling is not what the 8,690 measures**. The lever is identical
   on both sides.

3. **The only difference is whether SUP-21's units count.** SUP-21 is the
   cheapest certified supplier at 118/unit. Excluding its 700 units forces the
   plan onto SUP-42's full 700 at 132, plus SUP-55 and SUP-37 at 126 and 141.

4. **That difference crosses the approval threshold.** 143,320 is under
   150,000 and executes autonomously. 152,010 is over it by 2,010, G2 fires,
   `interrupt()` pauses the run and a human decides.

So verification is not a compliance step bolted onto the side. **It is the
reason this decision reaches a human at all.** An agent that took the dispatch
claim at face value would have spent 143,320 autonomously on a plan resting on
700 units nobody can confirm exist.

The guardrail output states it without ambiguity:

```
units counted: fired nothing · forced_escalation False
unconfirmed:   fired ['G2'] · forced_escalation True
    plan costs 152,010.00, exceeding the autonomous purchase threshold of 150,000 by 2,010.00
```

### How much margin this rides on

SUP-21 leads SUP-42 by **10.4 per unit** on risk-adjusted cost (129.2 against
139.6). While it stays cheapest adjusted, the counted-units plan is 143,320
and the delta is 8,690. A reputation penalty above 0.26 would flip that
ranking and change the demo. Recorded as finding 5 in
[`demo/HIDDEN_TEST_RESULTS.md`](../demo/HIDDEN_TEST_RESULTS.md); if seed
prices move, that is the number to re-check.

---

## Act 3 coda — and then costs rise

H-09 raises every alternate 40%. The plan re-solves to 212,814, still feasible
on the reschedule rung, and `/approval/check` independently agrees with the
solver's own `requires_approval`. The escalation is recorded to the simulated
ERP as `record_escalation`.
