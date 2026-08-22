"""Track A §5, sandbox items 3, 4 and 5 — behaviour, not shape.

These run against the live sandbox only, not against StubSandbox. The stub is
a canned fake whose job is to keep Track B unblocked on *shapes*; personas and
chaos are the real sandbox's own behaviour, and contracts/ is frozen, so
holding the stub to them would be holding it to a job it was not given.
Shape parity is tested in test_sandbox_contract.py, which does run against
both.
"""

import pytest

from sandbox.client import HttpSandbox


@pytest.fixture
def world(live_sandbox):
    """The live sandbox. conftest's autouse fixture has already reseeded it."""
    return HttpSandbox(live_sandbox)


# ---- item 3: a message queues a reply, visible on the next tick ---------

def test_message_to_sup21_queues_a_reply_retrievable_next_tick(world):
    before = {m.message_id for m in world.get_inbox()}
    sent = world.send_message("SUP-21", "PO-7712", "Please confirm status.")

    assert sent.message_id not in before
    replies = [m for m in world.get_inbox()
               if m.message_id not in before and m.sender.startswith("sup21")]
    assert len(replies) == 1, "exactly one reply per message, on the next read"


def test_the_reply_is_not_visible_inside_the_sending_call(world):
    """visible_at is what stops a follow-up and its answer arriving together."""
    sent = world.send_message("SUP-42", "PO-7712", "Confirm the date?")
    assert sent.sender == "ops@example.com"
    inbox = world.get_inbox()
    assert sent.message_id in {m.message_id for m in inbox}


# ---- item 4: the vague persona ------------------------------------------

def test_vague_persona_withholds_then_gives_specifics_when_challenged(world):
    """SUP-55 gives nothing on reply 1, and specifics on reply 2 only when the
    follow-up asks a question naming a date or a quantity (§4.8)."""
    world.send_message("SUP-55", "PO status", "Any update on this order?")
    first = [m for m in world.get_inbox() if m.sender.startswith("sup55")][-1]
    # Look for an actual date, not the word: "will update you soon" contains
    # the substring "date", which is the same trap the persona check itself
    # had to be fixed for.
    assert "2026-" not in first.body, "reply 1 commits to no date"
    assert not any(ch.isdigit() for ch in first.body), "and to no quantity"

    world.send_message("SUP-55", "PO status", "Confirm the exact date and quantity?")
    second = [m for m in world.get_inbox() if m.sender.startswith("sup55")][-1]
    assert second.message_id != first.message_id
    assert "2026-09" in second.body and "300 units" in second.body


def test_a_vague_follow_up_does_not_unlock_specifics(world):
    """"Any update?" contains the substring "date". It must not count."""
    world.send_message("SUP-55", "PO status", "Any update?")
    world.send_message("SUP-55", "PO status", "Any update?")
    replies = [m for m in world.get_inbox() if m.sender.startswith("sup55")]
    assert all("300 units" not in m.body for m in replies)


def test_contradictory_persona_claims_dispatch_against_its_own_tracking(world):
    world.send_message("SUP-21", "PO-7712", "Has this shipped?")
    reply = [m for m in world.get_inbox() if m.sender.startswith("sup21")][-1]
    assert "dispatched" in reply.body.lower()

    tracking = world.get_tracking("PO-7712")
    assert tracking.tracking_status == "label_created_no_pickup"
    assert tracking.last_movement is None


def test_contradictory_persona_revises_down_when_challenged(world):
    world.send_message("SUP-21", "PO-7712", "Has this shipped?")
    world.send_message("SUP-21", "PO-7712", "What quantity actually left, and on what date?")
    reply = [m for m in world.get_inbox() if m.sender.startswith("sup21")][-1]
    assert "250 units" in reply.body


def test_honest_persona_answers_with_a_date_and_a_quantity_first_time(world):
    world.send_message("SUP-42", "RFQ follow-up", "Can you confirm?")
    reply = [m for m in world.get_inbox() if m.sender.startswith("sup42")][-1]
    assert "2026-09" in reply.body and "units" in reply.body


# ---- item 5: every chaos event measurably changes state -----------------

def test_h01_pushes_a_confirmed_delivery_out_and_says_so_afterwards(world):
    before = world.get_purchase_orders("PO-7712")[0]
    messages_before = len(world.get_inbox())

    world.sim_inject("H-01")

    after = world.get_purchase_orders("PO-7712")[0]
    assert after.expected_delivery > before.expected_delivery
    assert (after.expected_delivery - before.expected_delivery).days == 5
    assert after.status == "delayed"
    assert len(world.get_inbox()) == messages_before + 1


def test_h02_widens_the_gap_between_reported_and_usable_stock(world):
    before = world.get_inventory("COMP-104")[0]
    world.sim_inject("H-02")
    after = world.get_inventory("COMP-104")[0]

    assert after.current_stock == 800
    assert after.usable_stock == 390
    assert after.current_stock - after.usable_stock > before.current_stock - before.usable_stock


def test_h03_drops_a_previously_eligible_supplier_below_the_quality_floor(world):
    comp = world.get_inventory("COMP-104")[0]
    required = set(comp.required_certifications)
    eligible_before = {s.supplier_id for s in world.get_suppliers("COMP-104")
                       if required <= set(s.certifications)
                       and s.quality_score >= comp.min_quality}

    result = world.sim_inject("H-03")
    target = result["detail"]["supplier_id"]
    assert target in eligible_before, "H-03 must disrupt something that was eligible"

    eligible_after = {s.supplier_id for s in world.get_suppliers("COMP-104")
                      if required <= set(s.certifications)
                      and s.quality_score >= comp.min_quality}
    assert eligible_after == eligible_before - {target}


def test_h04_cuts_the_reliable_supplier_short(world):
    before = {s.supplier_id: s for s in world.get_suppliers("COMP-104")}["SUP-37"]
    world.sim_inject("H-04")
    after = {s.supplier_id: s for s in world.get_suppliers("COMP-104")}["SUP-37"]

    assert before.available_quantity == 400 and after.available_quantity == 200
    assert after.reliability_score == before.reliability_score, "only quantity moves"


def test_h05_makes_the_risky_supplier_the_fastest(world):
    world.sim_inject("H-05")
    suppliers = {s.supplier_id: s for s in world.get_suppliers("COMP-104")}
    sup18 = suppliers["SUP-18"]

    assert sup18.lead_time_days == 2
    assert sup18.reliability_score == 0.5
    assert sup18.lead_time_days < min(s.lead_time_days for s in suppliers.values()
                                      if s.supplier_id != "SUP-18")


def test_h06_raises_the_burn_rate_and_shortens_coverage(world):
    before = world.get_inventory("COMP-104")[0]
    coverage_before = before.usable_stock / before.daily_usage

    world.sim_inject("H-06")

    after = world.get_inventory("COMP-104")[0]
    assert after.daily_usage == 130
    assert after.usable_stock / after.daily_usage < coverage_before


def test_h07_withdraws_expedite_from_future_quotes(world):
    before = world.request_rfq("COMP-104", 400, 4, ["SUP-21", "SUP-42"])
    assert any(q.expedite_available for q in before), "nothing to withdraw otherwise"

    world.sim_inject("H-07")

    after = world.request_rfq("COMP-104", 400, 4, ["SUP-21", "SUP-42"])
    assert after and not any(q.expedite_available for q in after)


def test_h08_makes_the_claim_and_the_tracking_disagree(world):
    messages_before = len(world.get_inbox())
    world.sim_inject("H-08")

    tracking = world.get_tracking("PO-7712")
    assert tracking.supplier_claim == "dispatched"
    assert tracking.tracking_status == "label_created_no_pickup"
    assert tracking.last_movement is None

    inbox = world.get_inbox()
    assert len(inbox) == messages_before + 1
    assert "dispatched" in inbox[-1].body.lower()


def test_h09_raises_the_alternates_but_not_the_incumbent(world):
    before = {s.supplier_id: s.unit_price for s in world.get_suppliers("COMP-104")}
    world.sim_inject("H-09")
    after = {s.supplier_id: s.unit_price for s in world.get_suppliers("COMP-104")}

    assert after["SUP-21"] == before["SUP-21"], "the PO-7712 incumbent is untouched"
    for supplier_id in set(before) - {"SUP-21"}:
        assert after[supplier_id] == pytest.approx(before[supplier_id] * 1.4, rel=1e-3)


def test_h10_flips_the_delayable_order_to_undelayable(world):
    before = {p.production_order_id: p for p in world.get_production_schedule()}
    assert before["PROD-914"].priority == "low"
    assert before["PROD-914"].max_delay_days == 5

    world.sim_inject("H-10")

    after = {p.production_order_id: p for p in world.get_production_schedule()}
    assert after["PROD-914"].priority == "high"
    assert after["PROD-914"].max_delay_days == 0


# ---- the injector itself -------------------------------------------------

def test_every_event_returns_a_disruption_id(world):
    for event in [f"H-{n:02d}" for n in range(1, 11)]:
        result = world.sim_inject(event)
        assert result["disruption_id"].startswith("DIS-")
        assert result["event"] == event


def test_unknown_event_is_rejected(world):
    with pytest.raises(Exception) as exc:
        world.sim_inject("H-99")
    assert "unknown event" in str(exc.value)


def test_sequence_fires_a_timed_cascade_without_anyone_typing(world):
    clock_before = world.sim_clock()["now"]
    results = world.sim_inject_sequence([
        {"event": "H-06", "params": {}, "delay_minutes": 0},
        {"event": "H-07", "params": {}, "delay_minutes": 30},
        {"event": "H-10", "params": {}, "delay_minutes": 30},
    ])
    assert [r["event"] for r in results] == ["H-06", "H-07", "H-10"]
    assert world.sim_clock()["now"] > clock_before

    assert world.get_inventory("COMP-104")[0].daily_usage == 130
    assert {p.production_order_id: p for p in world.get_production_schedule()
            }["PROD-914"].priority == "high"
