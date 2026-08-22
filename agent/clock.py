"""The simulated clock.

The sandbox is frozen at contracts.stub_sandbox.NOW (2026-09-02T10:00) so its
canned records stay deterministic. That is right for the records and wrong for
the trace: without a clock every audit event carries the same timestamp, the
30-simulated-second read cache never expires, and the CLI renders a run that
appears to happen in an instant.

So Track B keeps its own simulated clock, started at NOW to match the sandbox
and advanced explicitly — one tick per metered tool call, which is the only
thing in a run that plausibly takes time. Deterministic, replayable, and it
makes CACHE_TTL_SIM_SECONDS mean something.

Real wall-clock arrives with HttpSandbox at hour 12; only now() changes.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from contracts.stub_sandbox import NOW

TICK_SECONDS = 1.0          # simulated cost of one metered tool call


class SimClock:
    def __init__(self, start: datetime = NOW) -> None:
        self._start = start
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = TICK_SECONDS) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def elapsed(self) -> float:
        return (self._now - self._start).total_seconds()

    def reset(self, start: datetime | None = None) -> None:
        self._start = start or NOW
        self._now = self._start


CLOCK = SimClock()


def now() -> datetime:
    return CLOCK.now()


def advance(seconds: float = TICK_SECONDS) -> datetime:
    return CLOCK.advance(seconds)


def reset(start: datetime | None = None) -> None:
    CLOCK.reset(start)
