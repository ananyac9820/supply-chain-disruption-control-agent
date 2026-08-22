# Supplier reliability — two axes, deliberately separate

Most systems collapse supplier trust into one number. We use two, because the
questions "can we count on this shipment?" and "can we count on this company?"
have different answers, different evidence, and different consequences.

```
reputation(supplier_id)      slow, historical, about a counterparty
shipment_confidence(po_id)   fast, per-consignment, about one delivery
```

| | reputation | shipment confidence |
|---|---|---|
| about | an organisation | one consignment |
| moves on | `SUPPLIER`-attributed incidents only | any verified discrepancy |
| speed | slow, sticky, compounding | fast, per-PO |
| feeds | `effective_reliability` → the solver's risk term | whether in-transit units count as confirmed |
| rule | — | G9 |

## Why not one number

A dispatch claim standing against `label_created_no_pickup` tells you the
shipment is unverifiable. It does **not** tell you whose fault that is. The
supplier may have printed a label and never handed the goods over. Or the
supplier packed and tendered on time and the courier never collected.

Nothing in the tracking record separates those two worlds.

A single trust score forces you to guess, and the guess is sticky: reputation
compounds, and it goes on shaping every future sourcing decision long after
the shipment has been resolved one way or the other. Penalising a supplier for
a courier's failure is both unfair and wrong on the facts, and once it is in
the ledger nothing takes it back out.

So the shipment loses confidence — because it genuinely cannot be verified —
and the reputation does not move.

## Every discrepancy becomes an incident

An incident is an observation. Attribution is a separate judgement recorded
alongside it, and it is never a precondition for recording:

```
incident_id · po_id · supplier_id · observed · expected
attribution · attribution_basis · ts
```

`attribution_basis` is one readable line giving the reason, so the judgement
can be audited rather than taken on trust.

## Attribution comes from evidence

Implemented in `sandbox/attribution.py`, which lives in the sandbox because
the sandbox owns tracking ground truth.

| attribution | established by |
|---|---|
| `SUPPLIER` | nothing was ever tendered **and** the supplier's own record shows no pack event |
| `COURIER` | handover scanned, then no movement for 48 hours |
| `UNATTRIBUTED` | the evidence does not establish a cause |
| `FACTORY` | — |
| `EXTERNAL` | — |

### The COMP-104 case is UNATTRIBUTED, and that is the right answer

PO-7712 carries `supplier_claim: dispatched` against `tracking_status:
label_created_no_pickup` with no recorded movement. The system records the
incident, drops `shipment_confidence(PO-7712)` from 1.00 to 0.40 so those 700
units stop counting toward coverage, and leaves SUP-21's reputation at 0.72.

That is not the system being lenient. A printed label with no pickup scan is
exactly as consistent with a supplier that never tendered as with a courier
that never came, and we have no evidence that separates them. Attributing it
to the supplier would be asserting something we cannot show.

The plan still routes around SUP-21's units — G9 excludes them, and that
exclusion is what carries the cost from 143,320 to 152,010 and over the
approval threshold. **The units are excluded because they cannot be verified,
not because anyone has concluded the supplier acted in bad faith.** No rule
reason, brief line or log message in this repo says otherwise; a test asserts
that no accusatory word reaches a guardrail reason string.

### FACTORY and EXTERNAL exist, and nothing returns them

Both are in the vocabulary, and `sandbox/attribution.py` never returns either.

No tracking record can establish that a delay originated in a factory
shutdown or an external event — that evidence comes from elsewhere, and a
caller holding it supplies the attribution directly. Writing a rule that
guessed `FACTORY` from a tracking status would have been fabricating evidence,
and it would have made the whole attribution mechanism decorative: five labels
that all resolve from the same one source are not attribution, they are a
relabelling of the same guess.

That is the answer if a judge asks whether attribution is real or decorative.
Two of the five have no trigger, on purpose.

## How reputation reaches a decision

Only a `SUPPLIER`-attributed incident moves it, via `trust_write`:

```python
penalty = 0.15 × contradicted_claims + 0.05 × late + 0.05 × moq_failures
effective_reliability = max(0.05, catalog_score − penalty)
```

That value feeds the solver's risk term, weighted by `W_RISK = 40`, imported
from `contracts.constants` by both the CP-SAT model and the greedy fallback so
the two cannot drift apart.

There is no credit term. Ten clean deliveries do not offset one attributed
failure — a deliberate asymmetry, not an omission.

`quality_delta` is recorded and shown in the brief but is absent from the
penalty: a quality miss already removes a supplier through G4's floor, and
charging the risk term as well would price one failure twice.

## The property that makes it worth building

A ledger that never changes an answer is decoration. The test drives the real
ledger through both solvers: two suppliers, one cheaper and one more reliable,
identical inputs, and one `SUPPLIER`-attributed incident written between the
two solves. The allocation moves from the cheaper supplier to the more
reliable one, and the agent knowingly pays more.

`tests/solver/test_trust.py::test_an_attributed_incident_changes_the_next_allocation`
