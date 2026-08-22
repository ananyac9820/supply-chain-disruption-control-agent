"""The tool-call ledger — budget, read cache, necessity enforcement.

Three properties that matter (§4.2):

  1. `necessity` is required. No default, no empty string. If the planner
     cannot say why it is calling a tool, it should not call it.
  2. Cache hits do not decrement the budget and are logged as avoided.
     "11 used / 15 budget / 3 avoided" is the visible evidence for Tool
     Efficiency, and it only means something if the counter is real.
  3. Exhaustion fails closed to escalation, never a silent retry loop.

Writes are never cached. A write also invalidates the reads it can change —
otherwise messaging a supplier and then re-reading the inbox inside the TTL
would serve the pre-message inbox and the agent would never see the reply.
That is a correctness bug wearing a cache's clothing, so the invalidation map
lives here next to the cache rather than in a node.

Pulled forward from hour 9 because agent/tools.py cannot meter without it;
hour 9 becomes a hardening and test pass rather than a first write.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from agent import clock
from agent.errors import MissingNecessity, ToolBudgetExhausted
from contracts.constants import CACHE_TTL_SIM_SECONDS, TOOL_BUDGET_PER_DISRUPTION

# Reads may be served from cache. check_approval is a POST but it is a pure
# function of (action, cost) in the sandbox, so it caches safely.
CACHEABLE = frozenset({
    "get_inventory", "get_purchase_orders", "get_suppliers",
    "get_production_schedule", "get_inbox", "get_tracking", "check_approval",
})

# Writes are never cached (§4.2).
WRITE_TOOLS = frozenset({"send_message", "request_rfq", "erp_update"})

# A write invalidates the reads whose answers it can change.
INVALIDATES: dict[str, frozenset[str]] = {
    "send_message": frozenset({"get_inbox"}),
    "request_rfq": frozenset(),
    "erp_update": frozenset({
        "get_purchase_orders", "get_production_schedule", "get_inventory",
    }),
}


def hash_args(kwargs: dict) -> str:
    """Stable across dict ordering, so the same call always hits the cache."""
    blob = json.dumps(kwargs, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


@dataclass
class ToolCallRecord:
    call_id: str
    tool: str
    args_hash: str
    necessity: str
    served_from_cache: bool
    disruption_id: str
    ts: datetime

    def as_dict(self) -> dict:
        return {"call_id": self.call_id, "tool": self.tool,
                "args_hash": self.args_hash, "necessity": self.necessity,
                "served_from_cache": self.served_from_cache,
                "disruption_id": self.disruption_id, "ts": self.ts.isoformat()}


@dataclass
class _CacheEntry:
    value: object
    stored_at: datetime


@dataclass
class ToolLedger:
    """Per-disruption budget and read cache."""

    disruption_id: str
    budget: int = TOOL_BUDGET_PER_DISRUPTION
    ttl_seconds: int = CACHE_TTL_SIM_SECONDS
    used: int = 0
    avoided: int = 0
    calls: list[ToolCallRecord] = field(default_factory=list)
    _cache: dict[str, _CacheEntry] = field(default_factory=dict)

    # ---- budget ------------------------------------------------------

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.used)

    def check_necessity(self, tool: str, necessity: str | None) -> str:
        if not necessity or not necessity.strip():
            raise MissingNecessity(
                f"{tool} was called without a necessity string; every tool "
                "call must record why it was made")
        return necessity.strip()

    def check_budget(self, tool: str, necessity: str) -> None:
        if self.remaining <= 0:
            raise ToolBudgetExhausted(tool, necessity)

    # ---- cache -------------------------------------------------------

    def key(self, tool: str, kwargs: dict) -> str:
        return f"{tool}:{hash_args(kwargs)}"

    def fresh(self, key: str) -> bool:
        entry = self._cache.get(key)
        if entry is None:
            return False
        age = (clock.now() - entry.stored_at).total_seconds()
        return age <= self.ttl_seconds

    def get(self, key: str):
        return self._cache[key].value

    def store(self, key: str, value) -> None:
        self._cache[key] = _CacheEntry(value=value, stored_at=clock.now())

    def invalidate(self, tools: frozenset[str]) -> int:
        """Drop cached reads a write has just made stale. Returns how many."""
        doomed = [k for k in self._cache if k.split(":", 1)[0] in tools]
        for k in doomed:
            del self._cache[k]
        return len(doomed)

    # ---- recording ---------------------------------------------------

    def record(self, tool: str, args_hash: str, necessity: str,
               served_from_cache: bool) -> ToolCallRecord:
        if served_from_cache:
            self.avoided += 1
        else:
            self.used += 1
        rec = ToolCallRecord(
            call_id=f"TC-{len(self.calls) + 1:03d}", tool=tool,
            args_hash=args_hash, necessity=necessity,
            served_from_cache=served_from_cache,
            disruption_id=self.disruption_id, ts=clock.now())
        self.calls.append(rec)
        return rec

    def summary(self) -> dict:
        return {"used": self.used, "budget": self.budget,
                "avoided": self.avoided, "remaining": self.remaining}


_LEDGERS: dict[str, ToolLedger] = {}


def get_ledger(disruption_id: str) -> ToolLedger:
    led = _LEDGERS.get(disruption_id)
    if led is None:
        led = ToolLedger(disruption_id=disruption_id)
        _LEDGERS[disruption_id] = led
    return led


def reset_ledger(disruption_id: str) -> ToolLedger:
    _LEDGERS.pop(disruption_id, None)
    return get_ledger(disruption_id)
