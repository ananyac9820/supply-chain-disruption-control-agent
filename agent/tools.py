"""The metered wrapper over the sandbox client.

Every sandbox call in Track B goes through call_tool. Nothing calls the client
directly — that is the rule that makes the tool ledger, the audit trail and the
Tool Efficiency counter true rather than aspirational.

The hour-12 merge is the one line below:

    SANDBOX = StubSandbox()  ->  SANDBOX = HttpSandbox("http://localhost:8000")

Both satisfy contracts.stub_sandbox.SandboxClient, so nothing else changes.
"""

from __future__ import annotations

from typing import Any

from agent import clock
from agent.audit import append_event
from agent.ledger import (CACHEABLE, INVALIDATES, WRITE_TOOLS, get_ledger,
                          hash_args)
from contracts.stub_sandbox import SandboxClient, StubSandbox

SANDBOX: SandboxClient = StubSandbox()      # -> HttpSandbox(...) at hour 12

# The ten endpoints, and how each is named in the trace.
ENDPOINTS = {
    "get_inventory": "GET /inventory",
    "get_purchase_orders": "GET /purchase-orders",
    "get_suppliers": "GET /suppliers",
    "get_production_schedule": "GET /production-schedule",
    "get_inbox": "GET /inbox",
    "get_tracking": "GET /tracking",
    "send_message": "POST /suppliers/{id}/message",
    "request_rfq": "POST /rfq",
    "check_approval": "POST /approval/check",
    "erp_update": "POST /erp/update",
}


def set_sandbox(client: SandboxClient) -> SandboxClient:
    """Swap the client. Used by tests and by the hour-12 merge."""
    global SANDBOX
    SANDBOX = client
    return SANDBOX


def endpoint_label(tool_name: str, kwargs: dict) -> str:
    """The human-readable endpoint for the trace and the audit trail."""
    base = ENDPOINTS.get(tool_name, tool_name)
    if tool_name == "get_tracking" and kwargs.get("po_id"):
        return f"GET /tracking/{kwargs['po_id']}"
    if tool_name == "get_inventory" and kwargs.get("component_id"):
        return f"GET /inventory/{kwargs['component_id']}"
    if tool_name == "get_purchase_orders" and kwargs.get("po_id"):
        return f"GET /purchase-orders/{kwargs['po_id']}"
    if tool_name == "get_suppliers" and kwargs.get("component_id"):
        return f"GET /suppliers?component_id={kwargs['component_id']}"
    if tool_name == "send_message" and kwargs.get("supplier_id"):
        return f"POST /suppliers/{kwargs['supplier_id']}/message"
    return base


def call_tool(state: dict, tool_name: str, necessity: str, **kwargs) -> Any:
    """Meter, cache, record and dispatch one sandbox call.

    Mutates state["tools_called"] and state["tool_budget_remaining"] in place;
    node 3 passes a working copy and returns the changed keys.

    Raises ToolBudgetExhausted (G10) rather than retrying, and MissingNecessity
    if the caller cannot say why it is making the call.
    """
    disruption_id = state.get("disruption_id") or "DIS-000"
    ledger = get_ledger(disruption_id)
    necessity = ledger.check_necessity(tool_name, necessity)

    if tool_name not in ENDPOINTS:
        raise ValueError(f"unknown tool: {tool_name}")

    label = endpoint_label(tool_name, kwargs)
    key = ledger.key(tool_name, kwargs)
    cacheable = tool_name in CACHEABLE and tool_name not in WRITE_TOOLS

    # ---- cache hit: no budget charged, logged as avoided ---------------
    if cacheable and ledger.fresh(key):
        rec = ledger.record(tool_name, hash_args(kwargs), necessity,
                            served_from_cache=True)
        _record(state, rec, label, necessity, ledger, cached=True)
        return ledger.get(key)

    # ---- real call: budget first, so exhaustion fails closed -----------
    ledger.check_budget(tool_name, necessity)
    result = getattr(SANDBOX, tool_name)(**kwargs)
    clock.advance()                       # a metered call costs simulated time

    if cacheable:
        ledger.store(key, result)
    invalidated = 0
    if tool_name in WRITE_TOOLS:
        invalidated = ledger.invalidate(INVALIDATES.get(tool_name, frozenset()))

    rec = ledger.record(tool_name, hash_args(kwargs), necessity,
                        served_from_cache=False)
    _record(state, rec, label, necessity, ledger, cached=False,
            invalidated=invalidated)
    return result


def _record(state: dict, rec, label: str, necessity: str, ledger,
            *, cached: bool, invalidated: int = 0) -> None:
    """Write the call into state and into the audit trail."""
    entry = rec.as_dict() | {"endpoint": label}
    state["tools_called"] = list(state.get("tools_called") or []) + [entry]
    state["tool_budget_remaining"] = ledger.remaining

    detail = {"endpoint": label, "served_from_cache": cached,
              "budget_used": ledger.used, "budget_total": ledger.budget,
              "budget_avoided": ledger.avoided, "args_hash": rec.args_hash}
    if invalidated:
        detail["cache_invalidated"] = invalidated
    state["audit_events"] = append_event(
        state, type="tool_call", actor="investigator",
        summary=label + ("  (cache hit, budget not charged)" if cached else ""),
        detail=detail, tools_used=[label], necessity=necessity)
