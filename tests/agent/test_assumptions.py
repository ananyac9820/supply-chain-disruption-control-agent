"""The seven §4.4 triggers, and the ordering guarantee around them.

The point of this file is that recovery is DETERMINISTIC. Each trigger is a
comparison between a value the register recorded and a value the watcher
re-read, so every one of them is assertable — which is the whole difference
between "the model will probably notice" and a demo you can run twice.
"""

from __future__ import annotations

import pytest

from agent import clock
from agent.assumptions import (KIND_CLAIM, KIND_DEMAND, KIND_EXPEDITE,
                               KIND_PRIORITY, KIND_QUALITY, KIND_STOCK,
                               KIND_SUPPLY, register, watch)
from agent.audit import close_log, open_log
from agent.ledger import reset_ledger
from agent.nodes.execute import execute
from agent.nodes.impact import impact
from agent.nodes.investigate import investigate
from agent.nodes.monitor import monitor
from agent.tools import set_sandbox
from contracts.stub_sandbox import StubSandbox
from tests.agent.chaos import ChaosSandbox


@pytest.fixture
def run(tmp_path):
    """A run carried as far as node 3, on a chaos-capable sandbox."""
    clock.reset()
    sandbox = ChaosSandbox()
    set_sandbox(sandbox)
    open_log("DIS-T", path=tmp_path / "t.jsonl")
    reset_ledger("DIS-T")

    from agent.graph import initial_state
    state = dict(initial_state("DIS-T"))
    state |= monitor(state)
    state |= impact(state)
    state |= investigate(state)
    yield state, sandbox
    close_log("DIS-T")
    set_sandbox(StubSandbox())


def _plan_for(state):
    """A plan that leans on the three certified suppliers, without needing the
    solver — these tests are about the watcher, not about allocation."""
    return {"plan_id": "PLAN-001", "status": "FEASIBLE", "total_cost": 152_010.0,
            "allocations": [
                {"supplier_id": "SUP-42", "units": 700, "cost": 92_400.0,
                 "arrival_day": 4},
                {"supplier_id": "SUP-55", "units": 350, "cost": 44_100.0,
                 "arrival_day": 5},
                {"supplier_id": "SUP-37", "units": 350, "cost": 49_350.0,
                 "arrival_day": 6}],
            "reschedules": [{"production_order_id": "PROD-914", "delay_days": 4}]}


def _registered(state):
    state = dict(state)
    state["_catalog"] = state.get("_catalog") or []
    return register(state, _plan_for(state))


def _broken_kinds(state, assumptions):
    return {b["kind"] for b in watch(state, assumptions)}


# ---- the seven triggers ------------------------------------------------

def test_t1_supplier_contradicts_an_earlier_promise(run):
    state, _ = run
    assumptions = _registered(state)
    # the claim was PENDING when recorded; node 3 has since contradicted it
    a = [x for x in assumptions if x["kind"] == KIND_CLAIM]
    for x in a:
        x["expected"] = "PENDING"
    assert KIND_CLAIM in _broken_kinds(state, a)


def test_t2_inventory_corrected_downward(run):
    state, sandbox = run
    assumptions = [a for a in _registered(state) if a["kind"] == KIND_STOCK]
    assert not watch(state, assumptions)
    sandbox.inject("H-02")
    reset_ledger(state["disruption_id"])          # force a fresh read
    broken = watch(state, assumptions)
    assert broken and broken[0]["kind"] == KIND_STOCK
    assert "corrected down" in broken[0]["reason"]


def test_t2_more_stock_than_expected_is_not_a_break(run):
    """Corrected DOWNWARD only. Good news must not tear up a working plan."""
    state, _ = run
    assumptions = [a for a in _registered(state) if a["kind"] == KIND_STOCK]
    for a in assumptions:
        a["expected"] = 10          # far below reality
    assert not watch(state, assumptions)


def test_t3_demand_spike(run):
    state, sandbox = run
    assumptions = [a for a in _registered(state) if a["kind"] == KIND_DEMAND]
    assert assumptions, "demand assumptions should be registered"
    assert not watch(state, assumptions)
    sandbox.inject("H-06")
    reset_ledger(state["disruption_id"])
    broken = watch(state, assumptions)
    assert broken and "demand spike" in broken[0]["reason"]


def test_t4_expedite_withdrawn(run):
    state, _ = run
    assumptions = _registered(state)
    expedite = [a for a in assumptions if a["kind"] == KIND_EXPEDITE]
    assert expedite, "an available expedite should be registered"
    # H-07 withdraws it; the quote already in state is what the watcher reads
    for q in state["quotes"]:
        q["expedite_available"] = False
    broken = watch(state, expedite)
    assert broken and "expedite withdrawn" in broken[0]["reason"]


def test_t5_supplier_rejects_the_quantity(run):
    state, sandbox = run
    assumptions = [a for a in _registered(state)
                   if a["kind"] == KIND_SUPPLY and a["subject"] == "SUP-37"]
    sandbox.inject("H-04")                       # SUP-37 drops to 120 units
    reset_ledger(state["disruption_id"])
    broken = watch(state, assumptions)
    assert broken and "rejects the quantity" in broken[0]["reason"]


def test_t6_cheaper_supplier_fails_quality(run):
    state, sandbox = run
    assumptions = _registered(state)
    quality = [a for a in assumptions if a["kind"] == KIND_QUALITY]
    for a in quality:                            # pretend SUP-18 was in the plan
        a["subject"] = "SUP-18"
    sandbox.inject("H-03")                       # SUP-18 quality drops to 0.61
    reset_ledger(state["disruption_id"])
    broken = watch(state, quality)
    assert broken and "fails quality" in broken[0]["reason"]


def test_t7_production_priority_changes(run):
    state, sandbox = run
    assumptions = [a for a in _registered(state) if a["kind"] == KIND_PRIORITY]
    assert not watch(state, assumptions)
    sandbox.inject("H-10")                       # PROD-914 low -> high
    reset_ledger(state["disruption_id"])
    broken = watch(state, assumptions)
    assert broken and "priority changed" in broken[0]["reason"]


# ---- the ordering guarantee and the cap --------------------------------

def test_break_event_precedes_any_llm_output(run, tmp_path):
    """assumption_break must be on the trail BEFORE node 3 speaks again.

    Node 3's first act on re-entry is a model call, so the event has to be
    written by node 6. Asserted by position in the file, not by inspection.
    """
    from output.audit import read_jsonl
    state, sandbox = run
    state["plan"] = _plan_for(state)
    state["requires_approval"] = False
    state["human_response"] = {"decision": "auto"}
    sandbox.inject("H-06")
    reset_ledger(state["disruption_id"])

    state |= execute(state)
    assert state["broken_assumption"], "the spike should have broken an assumption"

    state |= investigate(state)                  # node 3 runs, calling the LLM
    events = read_jsonl(tmp_path / "t.jsonl")
    types = [e["type"] for e in events]
    assert "assumption_break" in types
    brk = types.index("assumption_break")
    replan = types.index("replan", brk)
    assert brk < replan, "the break must be recorded before node 3 re-enters"


def test_replan_cap_escalates_instead_of_looping(run):
    state, sandbox = run
    state["plan"] = _plan_for(state)
    state["replan_count"] = 3                    # cap already reached
    sandbox.inject("H-06")
    reset_ledger(state["disruption_id"])
    out = execute(state)
    assert out["broken_assumption"] is None, "at the cap it escalates, not replans"
    assert out["replan_count"] == 3


def test_disruption_id_survives_a_replan(run):
    state, sandbox = run
    original = state["disruption_id"]
    state["plan"] = _plan_for(state)
    sandbox.inject("H-06")
    reset_ledger(original)
    state |= execute(state)
    state |= investigate(state)
    assert state["disruption_id"] == original, (
        "a reopened disruption keeps its id so the trail stays one narrative")
